from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol, Sequence

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramMigrateToChat,
    TelegramNetworkError,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramServerError,
    TelegramUnauthorizedError,
)
from aiogram.types import InlineKeyboardMarkup

from app.chat_migration_service import migrate_telegram_chat
from app.contest_service import ContestCompletedError, ContestNotFoundError
from app.prediction_reminders import (
    NoOpenPredictionRemindersError,
    PredictionReminderMessageTooLongError,
)
from app.prediction_reminder_store import (
    ClaimedReminderDelivery,
    PredictionReminderClaimLostError,
    ReminderRenderRequest,
    RenderedReminderPart,
    StoredReminderPart,
    claim_next_prediction_reminder_delivery,
    finish_prediction_reminder_retry,
    finish_prediction_reminder_success,
    finish_prediction_reminder_terminal,
    finish_prediction_reminder_unknown,
    load_prediction_reminder_parts,
    mark_prediction_reminder_part_sending,
    reconcile_prediction_reminder_occurrences,
    record_prediction_reminder_part_sent,
    refresh_prediction_reminder_recipients,
    renew_prediction_reminder_claim,
    store_prediction_reminder_parts,
)
from app.rich_publications import rich_message
from app.tma_entrypoint import create_tma_launch_keyboard


logger = logging.getLogger(__name__)
DEFAULT_REMINDER_WORKER_INTERVAL_SECONDS = 5.0
TELEGRAM_REMINDER_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class ReminderPreflightRequest:
    delivery_id: int
    contest_id: int
    telegram_chat_id: int


@dataclass(slots=True)
class TelegramReminderPreflightResult:
    telegram_chat_id: int
    reply_markup: InlineKeyboardMarkup


class ReminderPreflight(Protocol):
    async def prepare(self, request: ReminderPreflightRequest) -> object: ...


class ReminderRenderer(Protocol):
    def render(
        self, request: ReminderRenderRequest
    ) -> Sequence[RenderedReminderPart]: ...


class ReminderSender(Protocol):
    async def send(
        self,
        preflight_result: object,
        part: StoredReminderPart,
    ) -> int: ...


class TelegramReminderBot(Protocol):
    async def get_chat(self, chat_id: int) -> object: ...

    async def send_rich_message(self, chat_id: int, **kwargs: object) -> object: ...


class TelegramPredictionReminderAdapter:
    """Resolve the current chat and deliver rich-message parts to Telegram."""

    def __init__(
        self,
        *,
        bot: TelegramReminderBot,
        database_path: Path,
        bot_username: str,
        bot_token: str,
        timeout_seconds: float = TELEGRAM_REMINDER_TIMEOUT_SECONDS,
    ) -> None:
        self._bot = bot
        self._database_path = database_path
        self._bot_username = bot_username
        self._bot_token = bot_token
        self._timeout_seconds = timeout_seconds

    async def prepare(
        self,
        request: ReminderPreflightRequest,
    ) -> TelegramReminderPreflightResult:
        return await self._prepare_chat(request.telegram_chat_id)

    async def send(
        self,
        preflight_result: object,
        part: StoredReminderPart,
    ) -> int:
        if not isinstance(preflight_result, TelegramReminderPreflightResult):
            raise TypeError("Unexpected reminder preflight result.")
        message = await self._send_part(preflight_result, part)
        message_id = getattr(message, "message_id", None)
        if isinstance(message_id, bool) or not isinstance(message_id, int):
            raise RuntimeError("Telegram reminder response has no message id.")
        return message_id

    async def _prepare_chat(
        self,
        telegram_chat_id: int,
        *,
        migrated_from_chat_id: int | None = None,
    ) -> TelegramReminderPreflightResult:
        current_chat_id = telegram_chat_id
        source_chat_id = migrated_from_chat_id
        for _ in range(2):
            try:
                chat = await asyncio.wait_for(
                    self._bot.get_chat(current_chat_id),
                    timeout=self._timeout_seconds,
                )
            except TelegramMigrateToChat as error:
                source_chat_id = current_chat_id
                current_chat_id = error.migrate_to_chat_id
                migrate_telegram_chat(
                    database_path=self._database_path,
                    old_telegram_chat_id=source_chat_id,
                    new_telegram_chat_id=current_chat_id,
                )
                continue

            resolved_chat_id = int(getattr(chat, "id", current_chat_id))
            chat_title_value = getattr(chat, "title", None)
            chat_title = str(chat_title_value) if chat_title_value else None
            if resolved_chat_id != current_chat_id:
                source_chat_id = current_chat_id
                migrate_telegram_chat(
                    database_path=self._database_path,
                    old_telegram_chat_id=source_chat_id,
                    new_telegram_chat_id=resolved_chat_id,
                    new_chat_title=chat_title,
                )
            if source_chat_id is not None:
                migrate_telegram_chat(
                    database_path=self._database_path,
                    old_telegram_chat_id=source_chat_id,
                    new_telegram_chat_id=resolved_chat_id,
                    new_chat_title=chat_title,
                )
            chat_type_value = getattr(chat, "type", None)
            chat_type = getattr(chat_type_value, "value", chat_type_value)
            if not isinstance(chat_type, str) or not chat_type:
                raise RuntimeError("Telegram chat response has no chat type.")
            return TelegramReminderPreflightResult(
                telegram_chat_id=resolved_chat_id,
                reply_markup=create_tma_launch_keyboard(
                    database_path=self._database_path,
                    telegram_chat_id=resolved_chat_id,
                    chat_type=chat_type,
                    chat_title=chat_title,
                    bot_username=self._bot_username,
                    bot_token=self._bot_token,
                ),
            )
        raise RuntimeError("Telegram chat migrated more than once during preflight.")

    async def _send_part(
        self,
        preflight_result: TelegramReminderPreflightResult,
        part: StoredReminderPart,
    ) -> object:
        return await asyncio.wait_for(
            self._bot.send_rich_message(
                chat_id=preflight_result.telegram_chat_id,
                rich_message=rich_message(part.html),
                reply_markup=(
                    preflight_result.reply_markup if part.has_launch_button else None
                ),
            ),
            timeout=self._timeout_seconds,
        )


async def process_due_prediction_reminders(
    *,
    database_path: Path,
    preflight: ReminderPreflight,
    renderer: ReminderRenderer,
    sender: ReminderSender,
    now_utc: datetime | None = None,
    max_deliveries: int = 100,
) -> int:
    if isinstance(max_deliveries, bool) or max_deliveries <= 0:
        raise ValueError("max_deliveries must be a positive integer.")
    reconcile_prediction_reminder_occurrences(
        database_path=database_path,
        now_utc=now_utc,
    )
    processed = 0
    for _ in range(max_deliveries):
        delivery = claim_next_prediction_reminder_delivery(
            database_path=database_path,
            now_utc=now_utc,
        )
        if delivery is None:
            break
        processed += 1
        await _process_claimed_delivery(
            database_path=database_path,
            delivery=delivery,
            preflight=preflight,
            renderer=renderer,
            sender=sender,
            now_utc=now_utc,
        )
    return processed


async def _process_claimed_delivery(
    *,
    database_path: Path,
    delivery: ClaimedReminderDelivery,
    preflight: ReminderPreflight,
    renderer: ReminderRenderer,
    sender: ReminderSender,
    now_utc: datetime | None,
) -> None:
    try:
        prepared_chat = await preflight.prepare(
            ReminderPreflightRequest(
                delivery_id=delivery.id,
                contest_id=delivery.contest_id,
                telegram_chat_id=delivery.telegram_chat_id,
            )
        )
    except asyncio.CancelledError:
        raise
    except TelegramMigrateToChat as error:
        _record_chat_migration(
            database_path=database_path,
            old_chat_id=delivery.telegram_chat_id,
            error=error,
        )
        _finish_retry(
            database_path=database_path,
            delivery=delivery,
            part_number=None,
            error=error,
            retry_after_seconds=1,
            now_utc=now_utc,
        )
        return
    except TelegramRetryAfter as error:
        _finish_retry(
            database_path=database_path,
            delivery=delivery,
            part_number=None,
            error=error,
            retry_after_seconds=float(error.retry_after),
            now_utc=now_utc,
        )
        return
    except (asyncio.TimeoutError, TelegramNetworkError, TelegramServerError) as error:
        _finish_retry(
            database_path=database_path,
            delivery=delivery,
            part_number=None,
            error=error,
            retry_after_seconds=None,
            now_utc=now_utc,
        )
        return
    except _PERMANENT_TELEGRAM_ERRORS as error:
        finish_prediction_reminder_terminal(
            database_path=database_path,
            delivery=delivery,
            error=str(error),
            now_utc=now_utc,
        )
        return
    except Exception as error:
        logger.exception("Prediction reminder preflight failed.")
        _finish_retry(
            database_path=database_path,
            delivery=delivery,
            part_number=None,
            error=error,
            retry_after_seconds=None,
            now_utc=now_utc,
        )
        return

    try:
        if not renew_prediction_reminder_claim(
            database_path=database_path,
            delivery=delivery,
            now_utc=now_utc,
        ):
            return
        stored_parts = load_prediction_reminder_parts(
            database_path=database_path,
            delivery=delivery,
            now_utc=now_utc,
        )
        if not stored_parts:
            request = refresh_prediction_reminder_recipients(
                database_path=database_path,
                delivery=delivery,
                now_utc=now_utc,
            )
            if request is None:
                return
            rendered = tuple(renderer.render(request))
            stored_parts = store_prediction_reminder_parts(
                database_path=database_path,
                delivery=delivery,
                parts=rendered,
                now_utc=now_utc,
            )
    except asyncio.CancelledError:
        raise
    except PredictionReminderClaimLostError:
        return
    except _DETERMINISTIC_RENDER_ERRORS as error:
        logger.exception("Prediction reminder rendering failed.")
        finish_prediction_reminder_terminal(
            database_path=database_path,
            delivery=delivery,
            error=str(error),
            now_utc=now_utc,
        )
        return
    except Exception as error:
        logger.exception("Prediction reminder preparation failed temporarily.")
        _finish_retry(
            database_path=database_path,
            delivery=delivery,
            part_number=None,
            error=error,
            retry_after_seconds=None,
            now_utc=now_utc,
        )
        return

    for part in stored_parts:
        if part.status == "sent":
            continue
        if part.status != "pending":
            finish_prediction_reminder_unknown(
                database_path=database_path,
                delivery=delivery,
                error=f"Unexpected stored reminder part status: {part.status}.",
                part_number=part.part_number,
                now_utc=now_utc,
            )
            return
        if not renew_prediction_reminder_claim(
            database_path=database_path,
            delivery=delivery,
            now_utc=now_utc,
        ):
            return
        if not mark_prediction_reminder_part_sending(
            database_path=database_path,
            delivery=delivery,
            part_number=part.part_number,
            now_utc=now_utc,
        ):
            return
        try:
            telegram_message_id = await sender.send(prepared_chat, part)
        except asyncio.CancelledError:
            finish_prediction_reminder_unknown(
                database_path=database_path,
                delivery=delivery,
                error="Prediction reminder send was cancelled after it started.",
                part_number=part.part_number,
                now_utc=now_utc,
            )
            raise
        except TelegramMigrateToChat as error:
            _record_chat_migration(
                database_path=database_path,
                old_chat_id=delivery.telegram_chat_id,
                error=error,
            )
            _finish_retry(
                database_path=database_path,
                delivery=delivery,
                part_number=part.part_number,
                error=error,
                retry_after_seconds=1,
                now_utc=now_utc,
            )
            return
        except TelegramRetryAfter as error:
            _finish_retry(
                database_path=database_path,
                delivery=delivery,
                part_number=part.part_number,
                error=error,
                retry_after_seconds=float(error.retry_after),
                now_utc=now_utc,
            )
            return
        except _PERMANENT_TELEGRAM_ERRORS as error:
            finish_prediction_reminder_terminal(
                database_path=database_path,
                delivery=delivery,
                error=str(error),
                part_number=part.part_number,
                now_utc=now_utc,
            )
            return
        except (
            asyncio.TimeoutError,
            TelegramNetworkError,
            TelegramServerError,
        ) as error:
            finish_prediction_reminder_unknown(
                database_path=database_path,
                delivery=delivery,
                error=str(error),
                part_number=part.part_number,
                now_utc=now_utc,
            )
            return
        except Exception as error:
            logger.exception("Prediction reminder Telegram send failed ambiguously.")
            finish_prediction_reminder_unknown(
                database_path=database_path,
                delivery=delivery,
                error=str(error),
                part_number=part.part_number,
                now_utc=now_utc,
            )
            return
        if not record_prediction_reminder_part_sent(
            database_path=database_path,
            delivery=delivery,
            part_number=part.part_number,
            telegram_message_id=telegram_message_id,
            now_utc=now_utc,
        ):
            finish_prediction_reminder_unknown(
                database_path=database_path,
                delivery=delivery,
                error="Telegram accepted a reminder part but its id was not stored.",
                part_number=part.part_number,
                now_utc=now_utc,
            )
            return

    finish_prediction_reminder_success(
        database_path=database_path,
        delivery=delivery,
        now_utc=now_utc,
    )


async def run_prediction_reminder_worker(
    *,
    database_path: Path,
    preflight: ReminderPreflight,
    renderer: ReminderRenderer,
    sender: ReminderSender,
    interval_seconds: float = DEFAULT_REMINDER_WORKER_INTERVAL_SECONDS,
    max_deliveries: int = 100,
) -> None:
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive.")
    while True:
        try:
            await process_due_prediction_reminders(
                database_path=database_path,
                preflight=preflight,
                renderer=renderer,
                sender=sender,
                max_deliveries=max_deliveries,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Prediction reminder worker iteration failed.")
        await asyncio.sleep(interval_seconds)


_PERMANENT_TELEGRAM_ERRORS = (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNotFound,
    TelegramUnauthorizedError,
)
_DETERMINISTIC_RENDER_ERRORS = (
    ContestCompletedError,
    ContestNotFoundError,
    NoOpenPredictionRemindersError,
    PredictionReminderMessageTooLongError,
    ValueError,
)


def _finish_retry(
    *,
    database_path: Path,
    delivery: ClaimedReminderDelivery,
    part_number: int | None,
    error: Exception,
    retry_after_seconds: float | None,
    now_utc: datetime | None,
) -> None:
    finish_prediction_reminder_retry(
        database_path=database_path,
        delivery=delivery,
        part_number=part_number,
        error=str(error),
        retry_after_seconds=retry_after_seconds,
        now_utc=now_utc,
    )


def _record_chat_migration(
    *,
    database_path: Path,
    old_chat_id: int,
    error: TelegramMigrateToChat,
) -> None:
    try:
        migrate_telegram_chat(
            database_path=database_path,
            old_telegram_chat_id=old_chat_id,
            new_telegram_chat_id=error.migrate_to_chat_id,
        )
    except Exception:
        logger.exception(
            "Could not persist Telegram chat migration from reminder send."
        )
