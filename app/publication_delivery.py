from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import logging
from pathlib import Path
from typing import Protocol

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramServerError,
)

from app.contest_publications import (
    get_publication_chat_id,
    retired_part_fallback_text,
    withdrawal_fallback_text,
)
from app.database import database_connection
from app.publication_outbox import (
    ClaimedPublication,
    PublicationClaimState,
    StalePublicationRevision,
    inspect_claim_state,
    renew_current_claim,
    resolve_service_time,
    serialize_service_time,
)


LOGGER = logging.getLogger(__name__)
PUBLICATION_TELEGRAM_TIMEOUT_SECONDS = 30.0


class SentTelegramMessage(Protocol):
    message_id: int


class TelegramPublicationClient(Protocol):
    async def send_message(
        self, chat_id: int, text: str, *, parse_mode: str
    ) -> SentTelegramMessage: ...

    async def edit_message_text(
        self,
        text: str,
        *,
        chat_id: int,
        message_id: int,
        parse_mode: str,
    ) -> object: ...

    async def delete_message(self, chat_id: int, message_id: int) -> bool: ...


class PublicationDeliveryError(RuntimeError):
    pass


class ClaimLostError(PublicationDeliveryError):
    pass


class TemporaryDeliveryError(PublicationDeliveryError):
    def __init__(self, message: str, *, retry_after_seconds: float | None = None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class PermanentDeliveryError(PublicationDeliveryError):
    pass


@dataclass(frozen=True, slots=True)
class StoredMessagePart:
    part_number: int
    telegram_message_id: int
    content_hash: str
    content_text: str | None
    part_status: str


async def deliver_publication(
    *,
    bot: TelegramPublicationClient,
    database_path: Path,
    publication: ClaimedPublication,
    desired_messages: tuple[str, ...],
    timeout_seconds: float = PUBLICATION_TELEGRAM_TIMEOUT_SECONDS,
) -> None:
    chat_id = get_publication_chat_id(
        database_path=database_path,
        contest_id=publication.contest_id,
    )
    stored_parts = {
        part.part_number: part
        for part in _get_stored_parts(
            database_path=database_path,
            publication_id=publication.id,
        )
    }

    for part_number, message_text in enumerate(desired_messages):
        content_hash = _content_hash(message_text)
        stored_part = stored_parts.get(part_number)
        if (
            stored_part is not None
            and stored_part.part_status == "active"
            and stored_part.content_hash == content_hash
        ):
            continue

        if stored_part is None:
            await _send_new_part(
                bot=bot,
                database_path=database_path,
                publication=publication,
                chat_id=chat_id,
                part_number=part_number,
                text=message_text,
                content_hash=content_hash,
                timeout_seconds=timeout_seconds,
            )
            continue

        try:
            await _edit_part(
                bot=bot,
                database_path=database_path,
                publication=publication,
                chat_id=chat_id,
                part_number=part_number,
                message_id=stored_part.telegram_message_id,
                previous_text=stored_part.content_text,
                text=message_text,
                timeout_seconds=timeout_seconds,
            )
        except _MessageMissingError:
            await _send_replacement_part(
                bot=bot,
                database_path=database_path,
                publication=publication,
                chat_id=chat_id,
                part_number=part_number,
                text=message_text,
                content_hash=content_hash,
                timeout_seconds=timeout_seconds,
            )
        else:
            updated = _update_part_after_edit(
                database_path=database_path,
                publication=publication,
                part_number=part_number,
                content_hash=content_hash,
                content_text=message_text,
                part_status="active",
            )
            if not updated:
                await _handle_changed_claim_after_edit(
                    bot=bot,
                    database_path=database_path,
                    publication=publication,
                    chat_id=chat_id,
                    part_number=part_number,
                    message_id=stored_part.telegram_message_id,
                    previous_text=stored_part.content_text,
                    timeout_seconds=timeout_seconds,
                )

    fallback_text = (
        withdrawal_fallback_text(publication)
        if publication.desired_action == "withdraw"
        else retired_part_fallback_text()
    )
    for part_number in sorted(stored_parts):
        if part_number < len(desired_messages):
            continue
        await _retire_extra_part(
            bot=bot,
            database_path=database_path,
            publication=publication,
            chat_id=chat_id,
            part=stored_parts[part_number],
            fallback_text=fallback_text,
            timeout_seconds=timeout_seconds,
        )


class _MessageMissingError(RuntimeError):
    pass


async def _send_new_part(
    *,
    bot: TelegramPublicationClient,
    database_path: Path,
    publication: ClaimedPublication,
    chat_id: int,
    part_number: int,
    text: str,
    content_hash: str,
    timeout_seconds: float,
) -> None:
    await _require_renewed_claim(database_path=database_path, publication=publication)
    try:
        sent = await asyncio.wait_for(
            bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML"),
            timeout=timeout_seconds,
        )
    except Exception as error:
        _require_current_claim_after_call(
            database_path=database_path,
            publication=publication,
        )
        _raise_classified(error)
        raise AssertionError("unreachable")

    state = inspect_claim_state(
        database_path=database_path,
        publication=publication,
    )
    if state.status != "current":
        compensated_state = await _compensate_untracked_message(
            bot=bot,
            database_path=database_path,
            publication=publication,
            chat_id=chat_id,
            message_id=int(sent.message_id),
            timeout_seconds=timeout_seconds,
        )
        _raise_for_claim_state(
            compensated_state,
            when="after compensating a sent message",
        )

    try:
        saved = _save_new_part(
            database_path=database_path,
            publication=publication,
            part_number=part_number,
            telegram_message_id=int(sent.message_id),
            content_hash=content_hash,
            content_text=text,
        )
        if not saved:
            state = inspect_claim_state(
                database_path=database_path,
                publication=publication,
            )
            _raise_for_claim_state(state, when="before saving a message")
            raise ClaimLostError("Publication part could not be saved.")
    except Exception:
        await _compensate_untracked_message(
            bot=bot,
            database_path=database_path,
            publication=publication,
            chat_id=chat_id,
            message_id=int(sent.message_id),
            timeout_seconds=timeout_seconds,
        )
        raise


async def _send_replacement_part(
    *,
    bot: TelegramPublicationClient,
    database_path: Path,
    publication: ClaimedPublication,
    chat_id: int,
    part_number: int,
    text: str,
    content_hash: str,
    timeout_seconds: float,
) -> None:
    await _require_renewed_claim(database_path=database_path, publication=publication)
    try:
        sent = await asyncio.wait_for(
            bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML"),
            timeout=timeout_seconds,
        )
    except Exception as error:
        _require_current_claim_after_call(
            database_path=database_path,
            publication=publication,
        )
        _raise_classified(error)
        raise AssertionError("unreachable")

    state = inspect_claim_state(
        database_path=database_path,
        publication=publication,
    )
    if state.status != "current":
        compensated_state = await _compensate_untracked_message(
            bot=bot,
            database_path=database_path,
            publication=publication,
            chat_id=chat_id,
            message_id=int(sent.message_id),
            timeout_seconds=timeout_seconds,
        )
        _raise_for_claim_state(
            compensated_state,
            when="after compensating a replacement",
        )

    try:
        replaced = _replace_part(
            database_path=database_path,
            publication=publication,
            part_number=part_number,
            telegram_message_id=int(sent.message_id),
            content_hash=content_hash,
            content_text=text,
        )
    except Exception:
        await _compensate_untracked_message(
            bot=bot,
            database_path=database_path,
            publication=publication,
            chat_id=chat_id,
            message_id=int(sent.message_id),
            timeout_seconds=timeout_seconds,
        )
        raise
    if not replaced:
        await _compensate_untracked_message(
            bot=bot,
            database_path=database_path,
            publication=publication,
            chat_id=chat_id,
            message_id=int(sent.message_id),
            timeout_seconds=timeout_seconds,
        )
        state = inspect_claim_state(
            database_path=database_path,
            publication=publication,
        )
        _raise_for_claim_state(state, when="before saving a replacement")
        raise ClaimLostError("Publication replacement could not be saved.")


async def _edit_part(
    *,
    bot: TelegramPublicationClient,
    database_path: Path,
    publication: ClaimedPublication,
    chat_id: int,
    part_number: int,
    message_id: int,
    previous_text: str | None,
    text: str,
    timeout_seconds: float,
) -> None:
    await _require_renewed_claim(database_path=database_path, publication=publication)
    call_error: Exception | None = None
    try:
        await asyncio.wait_for(
            bot.edit_message_text(
                text,
                chat_id=chat_id,
                message_id=message_id,
                parse_mode="HTML",
            ),
            timeout=timeout_seconds,
        )
    except Exception as error:
        call_error = error

    state = inspect_claim_state(
        database_path=database_path,
        publication=publication,
    )
    if state.status == "stale":
        if state.publication_enabled is False:
            await _compensate_disabled_edit(
                bot=bot,
                database_path=database_path,
                publication=publication,
                chat_id=chat_id,
                part_number=part_number,
                message_id=message_id,
                previous_text=previous_text,
                timeout_seconds=timeout_seconds,
            )
        elif state.desired_action == "withdraw":
            await _compensate_stale_edit(
                bot=bot,
                database_path=database_path,
                publication=publication,
                chat_id=chat_id,
                part_number=part_number,
                message_id=message_id,
                timeout_seconds=timeout_seconds,
            )
        else:
            _mark_part_stale_with_owned_claim(
                database_path=database_path,
                publication=publication,
                part_number=part_number,
            )
        raise StalePublicationRevision(
            "Publication revision changed while editing a message."
        )
    if state.status == "lost":
        raise ClaimLostError("Publication claim was lost after editing a message.")

    if call_error is None:
        return
    if isinstance(call_error, TelegramBadRequest):
        message = str(call_error).lower()
        if "message is not modified" in message:
            return
        if _is_message_missing(message):
            raise _MessageMissingError from call_error
    if isinstance(call_error, TelegramNotFound):
        raise _MessageMissingError from call_error
    _raise_classified(call_error)


async def _retire_extra_part(
    *,
    bot: TelegramPublicationClient,
    database_path: Path,
    publication: ClaimedPublication,
    chat_id: int,
    part: StoredMessagePart,
    fallback_text: str,
    timeout_seconds: float,
) -> None:
    if part.part_status == "retired":
        return
    await _require_renewed_claim(database_path=database_path, publication=publication)
    call_error: Exception | None = None
    try:
        await asyncio.wait_for(
            bot.delete_message(chat_id=chat_id, message_id=part.telegram_message_id),
            timeout=timeout_seconds,
        )
    except Exception as error:
        call_error = error

    state = inspect_claim_state(
        database_path=database_path,
        publication=publication,
    )
    if state.status != "current":
        message_was_deleted = call_error is None or (
            isinstance(call_error, (TelegramNotFound, TelegramBadRequest))
            and (
                isinstance(call_error, TelegramNotFound)
                or _is_message_missing(str(call_error).lower())
            )
        )
        if state.status == "stale" and message_was_deleted:
            _delete_part_record_with_owned_claim(
                database_path=database_path,
                publication=publication,
                part_number=part.part_number,
            )
        _raise_for_claim_state(state, when="after deleting a message")
    if call_error is None:
        deleted = _delete_part_record(
            database_path=database_path,
            publication=publication,
            part_number=part.part_number,
        )
        if not deleted:
            _handle_changed_claim_after_deleted_message(
                database_path=database_path,
                publication=publication,
                part_number=part.part_number,
            )
        return

    if isinstance(call_error, (TelegramNotFound, TelegramBadRequest)):
        message = str(call_error).lower()
        if isinstance(call_error, TelegramNotFound) or _is_message_missing(message):
            deleted = _delete_part_record(
                database_path=database_path,
                publication=publication,
                part_number=part.part_number,
            )
            if not deleted:
                _handle_changed_claim_after_deleted_message(
                    database_path=database_path,
                    publication=publication,
                    part_number=part.part_number,
                )
            return
        await _neutralize_part(
            bot=bot,
            database_path=database_path,
            publication=publication,
            chat_id=chat_id,
            part=part,
            fallback_text=fallback_text,
            timeout_seconds=timeout_seconds,
        )
        return
    if isinstance(call_error, TelegramForbiddenError):
        _mark_part_terminal_failed(
            database_path=database_path,
            publication=publication,
            part_number=part.part_number,
            error=str(call_error),
        )
        LOGGER.warning("Could not retire Telegram publication part: %s", call_error)
        return
    _raise_classified(call_error)


async def _neutralize_part(
    *,
    bot: TelegramPublicationClient,
    database_path: Path,
    publication: ClaimedPublication,
    chat_id: int,
    part: StoredMessagePart,
    fallback_text: str,
    timeout_seconds: float,
) -> None:
    try:
        await _edit_part(
            bot=bot,
            database_path=database_path,
            publication=publication,
            chat_id=chat_id,
            part_number=part.part_number,
            message_id=part.telegram_message_id,
            previous_text=part.content_text,
            text=fallback_text,
            timeout_seconds=timeout_seconds,
        )
    except _MessageMissingError:
        _delete_part_record(
            database_path=database_path,
            publication=publication,
            part_number=part.part_number,
        )
    except PermanentDeliveryError as error:
        _mark_part_terminal_failed(
            database_path=database_path,
            publication=publication,
            part_number=part.part_number,
            error=str(error),
        )
        LOGGER.warning("Could not neutralize Telegram publication part: %s", error)
    else:
        updated = _update_part_after_edit(
            database_path=database_path,
            publication=publication,
            part_number=part.part_number,
            content_hash=_content_hash(fallback_text),
            content_text=fallback_text,
            part_status="retired",
        )
        if not updated:
            await _handle_changed_claim_after_edit(
                bot=bot,
                database_path=database_path,
                publication=publication,
                chat_id=chat_id,
                part_number=part.part_number,
                message_id=part.telegram_message_id,
                previous_text=part.content_text,
                timeout_seconds=timeout_seconds,
            )


async def _require_renewed_claim(
    *, database_path: Path, publication: ClaimedPublication
) -> None:
    state = renew_current_claim(
        database_path=database_path,
        publication=publication,
    )
    _raise_for_claim_state(state, when="before Telegram call")


def _require_current_claim_after_call(
    *, database_path: Path, publication: ClaimedPublication
) -> None:
    state = inspect_claim_state(
        database_path=database_path,
        publication=publication,
    )
    _raise_for_claim_state(state, when="after Telegram call")


def _handle_changed_claim_after_deleted_message(
    *,
    database_path: Path,
    publication: ClaimedPublication,
    part_number: int,
) -> None:
    state = inspect_claim_state(
        database_path=database_path,
        publication=publication,
    )
    if state.status == "stale":
        _delete_part_record_with_owned_claim(
            database_path=database_path,
            publication=publication,
            part_number=part_number,
        )
    _raise_for_claim_state(state, when="before saving a deleted message")


def _raise_for_claim_state(state: PublicationClaimState, *, when: str) -> None:
    if state.status == "current":
        return
    if state.status == "stale":
        raise StalePublicationRevision(f"Publication revision changed {when}.")
    raise ClaimLostError(f"Publication claim was lost {when}.")


async def _handle_changed_claim_after_edit(
    *,
    bot: TelegramPublicationClient,
    database_path: Path,
    publication: ClaimedPublication,
    chat_id: int,
    part_number: int,
    message_id: int,
    previous_text: str | None,
    timeout_seconds: float,
) -> None:
    state = inspect_claim_state(
        database_path=database_path,
        publication=publication,
    )
    if state.status == "stale" and state.publication_enabled is False:
        await _compensate_disabled_edit(
            bot=bot,
            database_path=database_path,
            publication=publication,
            chat_id=chat_id,
            part_number=part_number,
            message_id=message_id,
            previous_text=previous_text,
            timeout_seconds=timeout_seconds,
        )
    elif state.status == "stale" and state.desired_action == "withdraw":
        await _compensate_stale_edit(
            bot=bot,
            database_path=database_path,
            publication=publication,
            chat_id=chat_id,
            part_number=part_number,
            message_id=message_id,
            timeout_seconds=timeout_seconds,
        )
    elif state.status == "stale":
        _mark_part_stale_with_owned_claim(
            database_path=database_path,
            publication=publication,
            part_number=part_number,
        )
    _raise_for_claim_state(state, when="before saving an edited message")


async def _compensate_stale_edit(
    *,
    bot: TelegramPublicationClient,
    database_path: Path,
    publication: ClaimedPublication,
    chat_id: int,
    part_number: int,
    message_id: int,
    timeout_seconds: float,
) -> None:
    if not _stale_withdraw_is_owned(
        database_path=database_path,
        publication=publication,
    ):
        return
    try:
        await asyncio.wait_for(
            bot.delete_message(chat_id=chat_id, message_id=message_id),
            timeout=timeout_seconds,
        )
    except (TelegramNotFound, TelegramBadRequest) as error:
        if isinstance(error, TelegramNotFound) or _is_message_missing(
            str(error).lower()
        ):
            if _claim_is_owned(database_path=database_path, publication=publication):
                _delete_part_record_with_owned_claim(
                    database_path=database_path,
                    publication=publication,
                    part_number=part_number,
                )
            return
    except Exception:
        pass
    else:
        if _claim_is_owned(database_path=database_path, publication=publication):
            _delete_part_record_with_owned_claim(
                database_path=database_path,
                publication=publication,
                part_number=part_number,
            )
        return

    if not _stale_withdraw_is_owned(
        database_path=database_path,
        publication=publication,
    ):
        return
    fallback_text = withdrawal_fallback_text(publication)
    try:
        await asyncio.wait_for(
            bot.edit_message_text(
                fallback_text,
                chat_id=chat_id,
                message_id=message_id,
                parse_mode="HTML",
            ),
            timeout=timeout_seconds,
        )
    except Exception:
        LOGGER.exception(
            "Could not compensate stale edited Telegram message %s in chat %s.",
            message_id,
            chat_id,
        )
        return
    state = inspect_claim_state(
        database_path=database_path,
        publication=publication,
    )
    if state.status == "stale" and state.desired_action == "withdraw":
        _retire_part_with_owned_claim(
            database_path=database_path,
            publication=publication,
            part_number=part_number,
            content_hash=_content_hash(fallback_text),
            content_text=fallback_text,
        )
    elif state.status != "lost":
        _mark_part_stale_with_owned_claim(
            database_path=database_path,
            publication=publication,
            part_number=part_number,
        )


async def _compensate_disabled_edit(
    *,
    bot: TelegramPublicationClient,
    database_path: Path,
    publication: ClaimedPublication,
    chat_id: int,
    part_number: int,
    message_id: int,
    previous_text: str | None,
    timeout_seconds: float,
) -> None:
    if not _claim_is_owned(database_path=database_path, publication=publication):
        return
    if previous_text is not None:
        try:
            await asyncio.wait_for(
                bot.edit_message_text(
                    previous_text,
                    chat_id=chat_id,
                    message_id=message_id,
                    parse_mode="HTML",
                ),
                timeout=timeout_seconds,
            )
        except Exception:
            LOGGER.exception(
                "Could not restore disabled Telegram publication message %s in chat %s.",
                message_id,
                chat_id,
            )
        else:
            if not _claim_is_owned(
                database_path=database_path, publication=publication
            ):
                return
            restored = _restore_part_with_owned_claim(
                database_path=database_path,
                publication=publication,
                part_number=part_number,
                content_text=previous_text,
            )
            if restored:
                return

    fallback_text = withdrawal_fallback_text(publication)
    try:
        await asyncio.wait_for(
            bot.edit_message_text(
                fallback_text,
                chat_id=chat_id,
                message_id=message_id,
                parse_mode="HTML",
            ),
            timeout=timeout_seconds,
        )
    except Exception:
        LOGGER.exception(
            "Could not neutralize disabled Telegram publication message %s in chat %s.",
            message_id,
            chat_id,
        )
        return
    if _claim_is_owned(database_path=database_path, publication=publication):
        _retire_part_with_owned_claim(
            database_path=database_path,
            publication=publication,
            part_number=part_number,
            content_hash=_content_hash(fallback_text),
            content_text=fallback_text,
        )


def _stale_withdraw_is_owned(
    *, database_path: Path, publication: ClaimedPublication
) -> bool:
    state = inspect_claim_state(
        database_path=database_path,
        publication=publication,
    )
    return state.status == "stale" and state.desired_action == "withdraw"


def _claim_is_owned(*, database_path: Path, publication: ClaimedPublication) -> bool:
    return (
        inspect_claim_state(
            database_path=database_path,
            publication=publication,
        ).status
        != "lost"
    )


async def _compensate_untracked_message(
    *,
    bot: TelegramPublicationClient,
    database_path: Path,
    publication: ClaimedPublication,
    chat_id: int,
    message_id: int,
    timeout_seconds: float,
) -> PublicationClaimState:
    inspect_claim_state(
        database_path=database_path,
        publication=publication,
    )
    try:
        await asyncio.wait_for(
            bot.delete_message(chat_id=chat_id, message_id=message_id),
            timeout=timeout_seconds,
        )
    except Exception:
        LOGGER.exception(
            "Could not compensate untracked Telegram message %s in chat %s.",
            message_id,
            chat_id,
        )
    return inspect_claim_state(
        database_path=database_path,
        publication=publication,
    )


def _get_stored_parts(
    *, database_path: Path, publication_id: int
) -> tuple[StoredMessagePart, ...]:
    with database_connection(database_path) as connection:
        rows = connection.execute(
            """
            SELECT
                part_number,
                telegram_message_id,
                content_hash,
                content_text,
                part_status
            FROM contest_publication_messages
            WHERE publication_id = ?
            ORDER BY part_number
            """,
            (publication_id,),
        ).fetchall()
    return tuple(
        StoredMessagePart(
            part_number=int(row["part_number"]),
            telegram_message_id=int(row["telegram_message_id"]),
            content_hash=str(row["content_hash"]),
            content_text=(
                str(row["content_text"]) if row["content_text"] is not None else None
            ),
            part_status=str(row["part_status"]),
        )
        for row in rows
    )


def _save_new_part(
    *,
    database_path: Path,
    publication: ClaimedPublication,
    part_number: int,
    telegram_message_id: int,
    content_hash: str,
    content_text: str,
) -> bool:
    now_value = serialize_service_time(resolve_service_time())
    with database_connection(database_path) as connection:
        insertion = connection.execute(
            """
            INSERT INTO contest_publication_messages (
                publication_id,
                part_number,
                telegram_message_id,
                content_hash,
                content_text,
                part_status,
                sent_at,
                updated_at
            )
            SELECT ?, ?, ?, ?, ?, 'active', ?, ?
            WHERE EXISTS (
                SELECT 1 FROM contest_publications
                WHERE id = ?
                  AND claim_token = ?
                  AND desired_revision = ?
                  AND desired_action = ?
                  AND entity_id = ?
            )
            """,
            (
                publication.id,
                part_number,
                telegram_message_id,
                content_hash,
                content_text,
                now_value,
                now_value,
                publication.id,
                publication.claim_token,
                publication.desired_revision,
                publication.desired_action,
                publication.entity_id,
            ),
        )
        return insertion.rowcount == 1


def _replace_part(
    *,
    database_path: Path,
    publication: ClaimedPublication,
    part_number: int,
    telegram_message_id: int,
    content_hash: str,
    content_text: str,
) -> bool:
    now_value = serialize_service_time(resolve_service_time())
    with database_connection(database_path) as connection:
        update = connection.execute(
            """
            UPDATE contest_publication_messages
            SET
                telegram_message_id = ?,
                content_hash = ?,
                content_text = ?,
                part_status = 'active',
                last_error = NULL,
                sent_at = ?,
                updated_at = ?
            WHERE publication_id = ?
              AND part_number = ?
              AND EXISTS (
                  SELECT 1
                  FROM contest_publications
                  WHERE id = ?
                    AND claim_token = ?
                    AND desired_revision = ?
                    AND desired_action = ?
                    AND entity_id = ?
              )
            """,
            (
                telegram_message_id,
                content_hash,
                content_text,
                now_value,
                now_value,
                publication.id,
                part_number,
                publication.id,
                publication.claim_token,
                publication.desired_revision,
                publication.desired_action,
                publication.entity_id,
            ),
        )
        return update.rowcount == 1


def _update_part_after_edit(
    *,
    database_path: Path,
    publication: ClaimedPublication,
    part_number: int,
    content_hash: str,
    content_text: str,
    part_status: str,
) -> bool:
    now_value = serialize_service_time(resolve_service_time())
    with database_connection(database_path) as connection:
        update = connection.execute(
            """
            UPDATE contest_publication_messages
            SET
                content_hash = ?,
                content_text = ?,
                part_status = ?,
                last_error = NULL,
                updated_at = ?
            WHERE publication_id = ?
              AND part_number = ?
              AND EXISTS (
                  SELECT 1 FROM contest_publications
                  WHERE id = ?
                    AND claim_token = ?
                    AND desired_revision = ?
                    AND desired_action = ?
                    AND entity_id = ?
              )
            """,
            (
                content_hash,
                content_text,
                part_status,
                now_value,
                publication.id,
                part_number,
                publication.id,
                publication.claim_token,
                publication.desired_revision,
                publication.desired_action,
                publication.entity_id,
            ),
        )
        return update.rowcount == 1


def _delete_part_record(
    *,
    database_path: Path,
    publication: ClaimedPublication,
    part_number: int,
) -> bool:
    with database_connection(database_path) as connection:
        deletion = connection.execute(
            """
            DELETE FROM contest_publication_messages
            WHERE publication_id = ?
              AND part_number = ?
              AND EXISTS (
                  SELECT 1 FROM contest_publications
                  WHERE id = ?
                    AND claim_token = ?
                    AND desired_revision = ?
                    AND desired_action = ?
                    AND entity_id = ?
              )
            """,
            (
                publication.id,
                part_number,
                publication.id,
                publication.claim_token,
                publication.desired_revision,
                publication.desired_action,
                publication.entity_id,
            ),
        )
        return deletion.rowcount == 1


def _mark_part_terminal_failed(
    *,
    database_path: Path,
    publication: ClaimedPublication,
    part_number: int,
    error: str,
) -> None:
    now_value = serialize_service_time(resolve_service_time())
    with database_connection(database_path) as connection:
        connection.execute(
            """
            UPDATE contest_publication_messages
            SET
                content_hash = 'terminal-failed',
                part_status = 'terminal_failed',
                last_error = ?,
                updated_at = ?
            WHERE publication_id = ?
              AND part_number = ?
              AND EXISTS (
                  SELECT 1 FROM contest_publications
                  WHERE id = ?
                    AND claim_token = ?
                    AND desired_revision = ?
                    AND desired_action = ?
                    AND entity_id = ?
              )
            """,
            (
                error[:2000],
                now_value,
                publication.id,
                part_number,
                publication.id,
                publication.claim_token,
                publication.desired_revision,
                publication.desired_action,
                publication.entity_id,
            ),
        )


def _delete_part_record_with_owned_claim(
    *,
    database_path: Path,
    publication: ClaimedPublication,
    part_number: int,
) -> bool:
    with database_connection(database_path) as connection:
        deletion = connection.execute(
            """
            DELETE FROM contest_publication_messages
            WHERE publication_id = ?
              AND part_number = ?
              AND EXISTS (
                  SELECT 1 FROM contest_publications
                  WHERE id = ? AND claim_token = ?
              )
            """,
            (
                publication.id,
                part_number,
                publication.id,
                publication.claim_token,
            ),
        )
        return deletion.rowcount == 1


def _retire_part_with_owned_claim(
    *,
    database_path: Path,
    publication: ClaimedPublication,
    part_number: int,
    content_hash: str,
    content_text: str,
) -> bool:
    now_value = serialize_service_time(resolve_service_time())
    with database_connection(database_path) as connection:
        update = connection.execute(
            """
            UPDATE contest_publication_messages
            SET
                content_hash = ?,
                content_text = ?,
                part_status = 'retired',
                last_error = NULL,
                updated_at = ?
            WHERE publication_id = ?
              AND part_number = ?
              AND EXISTS (
                  SELECT 1 FROM contest_publications
                  WHERE id = ? AND claim_token = ?
              )
            """,
            (
                content_hash,
                content_text,
                now_value,
                publication.id,
                part_number,
                publication.id,
                publication.claim_token,
            ),
        )
        return update.rowcount == 1


def _restore_part_with_owned_claim(
    *,
    database_path: Path,
    publication: ClaimedPublication,
    part_number: int,
    content_text: str,
) -> bool:
    now_value = serialize_service_time(resolve_service_time())
    with database_connection(database_path) as connection:
        update = connection.execute(
            """
            UPDATE contest_publication_messages
            SET
                content_hash = ?,
                content_text = ?,
                part_status = 'active',
                last_error = NULL,
                updated_at = ?
            WHERE publication_id = ?
              AND part_number = ?
              AND EXISTS (
                  SELECT 1 FROM contest_publications
                  WHERE id = ? AND claim_token = ?
              )
            """,
            (
                _content_hash(content_text),
                content_text,
                now_value,
                publication.id,
                part_number,
                publication.id,
                publication.claim_token,
            ),
        )
        return update.rowcount == 1


def _mark_part_stale_with_owned_claim(
    *,
    database_path: Path,
    publication: ClaimedPublication,
    part_number: int,
) -> bool:
    now_value = serialize_service_time(resolve_service_time())
    with database_connection(database_path) as connection:
        update = connection.execute(
            """
            UPDATE contest_publication_messages
            SET content_hash = 'stale-edit', updated_at = ?
            WHERE publication_id = ?
              AND part_number = ?
              AND EXISTS (
                  SELECT 1 FROM contest_publications
                  WHERE id = ? AND claim_token = ?
              )
            """,
            (
                now_value,
                publication.id,
                part_number,
                publication.id,
                publication.claim_token,
            ),
        )
        return update.rowcount == 1


def _raise_classified(error: Exception) -> None:
    if isinstance(error, asyncio.TimeoutError):
        raise TemporaryDeliveryError("Telegram request timed out.") from error
    if isinstance(error, TelegramRetryAfter):
        raise TemporaryDeliveryError(
            str(error),
            retry_after_seconds=float(error.retry_after),
        ) from error
    if isinstance(error, (TelegramNetworkError, TelegramServerError)):
        raise TemporaryDeliveryError(str(error)) from error
    if isinstance(error, (TelegramForbiddenError, TelegramNotFound)):
        raise PermanentDeliveryError(str(error)) from error
    if isinstance(error, TelegramBadRequest):
        raise PermanentDeliveryError(str(error)) from error
    if isinstance(error, PublicationDeliveryError):
        raise error
    raise TemporaryDeliveryError(str(error)) from error


def _is_message_missing(message: str) -> bool:
    return "message to edit not found" in message or "message not found" in message


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
