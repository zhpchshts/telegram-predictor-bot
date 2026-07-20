from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.methods import SendMessage
import pytest

from app import publication_delivery, publication_worker
from app.database import database_connection, initialize_database
from app.publication_delivery import (
    ClaimLostError,
    TemporaryDeliveryError,
    deliver_publication,
)
from app.publication_outbox import (
    PUBLICATION_MAX_ATTEMPTS,
    PUBLICATION_MAX_BACKOFF_SECONDS,
    StalePublicationRevision,
    claim_next_publication,
    create_publication_if_enabled,
    finish_publication_failure,
    finish_publication_success,
    renew_claim,
    revise_existing_publication,
    serialize_service_time,
)


NOW = datetime(2026, 7, 19, 12, tzinfo=timezone.utc)


@dataclass(frozen=True, slots=True)
class SentMessage:
    message_id: int


class RecordingBot:
    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []
        self.edited: list[dict[str, object]] = []
        self.deleted: list[dict[str, int]] = []

    async def send_message(
        self, chat_id: int, text: str, *, parse_mode: str
    ) -> SentMessage:
        message_id = 1000 + len(self.sent)
        self.sent.append({"chat_id": chat_id, "text": text})
        return SentMessage(message_id)

    async def edit_message_text(
        self,
        text: str,
        *,
        chat_id: int,
        message_id: int,
        parse_mode: str,
    ) -> bool:
        self.edited.append({"chat_id": chat_id, "message_id": message_id, "text": text})
        return True

    async def delete_message(self, chat_id: int, message_id: int) -> bool:
        self.deleted.append({"chat_id": chat_id, "message_id": message_id})
        return True


def test_renew_lease_extends_claim_expiration(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"
    _seed_publications(database_path)
    claim = claim_next_publication(database_path=database_path, now_utc=NOW)
    assert claim is not None

    assert renew_claim(
        database_path=database_path,
        publication_id=claim.id,
        claim_token=claim.claim_token,
        now_utc=NOW + timedelta(seconds=60),
        lease_seconds=120,
    )
    with database_connection(database_path) as connection:
        expires_at = connection.execute(
            "SELECT claim_expires_at FROM contest_publications WHERE id = ?",
            (claim.id,),
        ).fetchone()[0]
    assert expires_at == serialize_service_time(NOW + timedelta(seconds=180))


def test_claim_loss_before_telegram_call_sends_nothing(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"
    _seed_publications(database_path)
    claim = claim_next_publication(database_path=database_path)
    assert claim is not None
    _release_claim(database_path, claim.id)
    bot = RecordingBot()

    with pytest.raises(ClaimLostError):
        asyncio.run(
            deliver_publication(
                bot=bot,
                database_path=database_path,
                publication=claim,
                desired_messages=("message",),
            )
        )
    assert bot.sent == []


def test_stale_revision_before_telegram_call_is_settled_without_send(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    contest_id = _seed_publications(database_path)
    claim = claim_next_publication(database_path=database_path)
    assert claim is not None
    with database_connection(database_path) as connection:
        assert revise_existing_publication(
            connection,
            contest_id=contest_id,
            publication_type="contest_completed",
            entity_id=1,
            event_id=2,
            desired_action="withdraw",
            now_utc=NOW,
        )
    bot = RecordingBot()

    with pytest.raises(StalePublicationRevision):
        asyncio.run(
            deliver_publication(
                bot=bot,
                database_path=database_path,
                publication=claim,
                desired_messages=("stale",),
            )
        )
    assert bot.sent == []
    assert finish_publication_success(
        database_path=database_path,
        publication=claim,
        status="published",
    )
    with database_connection(database_path) as connection:
        row = connection.execute(
            """
            SELECT desired_revision, settled_revision, desired_action,
                   delivery_status, claim_token
            FROM contest_publications
            """
        ).fetchone()
    assert tuple(row) == (2, 1, "withdraw", "pending", None)


def test_claim_loss_after_telegram_call_compensates_message(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"
    _seed_publications(database_path)
    claim = claim_next_publication(database_path=database_path)
    assert claim is not None

    class ClaimLosingBot(RecordingBot):
        async def send_message(
            self, chat_id: int, text: str, *, parse_mode: str
        ) -> SentMessage:
            sent = await super().send_message(chat_id, text, parse_mode=parse_mode)
            _release_claim(database_path, claim.id)
            return sent

    bot = ClaimLosingBot()
    with pytest.raises(ClaimLostError):
        asyncio.run(
            deliver_publication(
                bot=bot,
                database_path=database_path,
                publication=claim,
                desired_messages=("message",),
            )
        )
    assert bot.deleted == [{"chat_id": -1001, "message_id": 1000}]


def test_sqlite_failure_after_send_compensates_and_is_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "predictor.db"
    _seed_publications(database_path)
    bot = RecordingBot()

    def fail_save(**kwargs) -> bool:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(publication_delivery, "_save_new_part", fail_save)
    monkeypatch.setattr(
        publication_worker,
        "render_publication_messages",
        lambda **kwargs: ("message",),
    )
    assert (
        asyncio.run(
            publication_worker.process_due_contest_publications(
                bot=bot,
                database_path=database_path,
                max_publications=1,
            )
        )
        == 0
    )
    assert bot.deleted == [{"chat_id": -1001, "message_id": 1000}]
    with database_connection(database_path) as connection:
        row = connection.execute(
            """
            SELECT delivery_status, attempt_count, last_error
            FROM contest_publications
            """
        ).fetchone()
    assert tuple(row) == ("pending", 1, "database is locked")


def test_replacement_save_exception_compensates_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "predictor.db"
    contest_id = _seed_publications(database_path)
    original_bot = RecordingBot()
    _deliver_and_finish(database_path, original_bot, ("old",))
    claim = _revise_and_claim(database_path, contest_id)

    class MissingMessageBot(RecordingBot):
        async def edit_message_text(self, *args, **kwargs) -> bool:
            raise TelegramBadRequest(
                method=SendMessage(chat_id=-1001, text="x"),
                message="message to edit not found",
            )

    def fail_replace(**kwargs) -> bool:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(publication_delivery, "_replace_part", fail_replace)
    bot = MissingMessageBot()
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        asyncio.run(
            deliver_publication(
                bot=bot,
                database_path=database_path,
                publication=claim,
                desired_messages=("new",),
            )
        )
    assert bot.deleted == [{"chat_id": -1001, "message_id": 1000}]


def test_telegram_retry_after_preserves_retry_delay(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"
    _seed_publications(database_path)
    claim = claim_next_publication(database_path=database_path)
    assert claim is not None

    class RetryAfterBot(RecordingBot):
        async def send_message(self, *args, **kwargs) -> SentMessage:
            raise TelegramRetryAfter(
                method=SendMessage(chat_id=-1001, text="x"),
                message="retry later",
                retry_after=37,
            )

    with pytest.raises(TemporaryDeliveryError) as error:
        asyncio.run(
            deliver_publication(
                bot=RetryAfterBot(),
                database_path=database_path,
                publication=claim,
                desired_messages=("message",),
            )
        )
    assert error.value.retry_after_seconds == 37


def test_temporary_retry_and_permanent_terminal_failure(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"
    _seed_publications(database_path)
    claim = claim_next_publication(database_path=database_path, now_utc=NOW)
    assert claim is not None
    assert finish_publication_failure(
        database_path=database_path,
        publication=claim,
        error="network",
        permanent=False,
        now_utc=NOW,
    )
    with database_connection(database_path) as connection:
        retry = connection.execute(
            """
            SELECT delivery_status, settled_revision, next_attempt_at
            FROM contest_publications
            """
        ).fetchone()
    assert retry["delivery_status"] == "pending"
    assert retry["settled_revision"] == 0
    assert retry["next_attempt_at"] is not None

    next_claim = claim_next_publication(
        database_path=database_path,
        now_utc=NOW + timedelta(hours=1),
    )
    assert next_claim is not None
    assert finish_publication_failure(
        database_path=database_path,
        publication=next_claim,
        error="forbidden",
        permanent=True,
        now_utc=NOW + timedelta(hours=1),
    )
    with database_connection(database_path) as connection:
        terminal = connection.execute(
            """
            SELECT delivery_status, settled_revision, next_attempt_at, last_error
            FROM contest_publications
            """
        ).fetchone()
    assert tuple(terminal) == ("terminal_failed", 1, None, "forbidden")


def test_contest_queue_is_held_during_retry_and_released_after_terminal(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    _seed_publications(database_path, count=2)
    first = claim_next_publication(database_path=database_path, now_utc=NOW)
    assert first is not None
    assert claim_next_publication(database_path=database_path, now_utc=NOW) is None
    assert finish_publication_failure(
        database_path=database_path,
        publication=first,
        error="network",
        permanent=False,
        retry_after_seconds=3600,
        now_utc=NOW,
    )
    assert claim_next_publication(database_path=database_path, now_utc=NOW) is None
    retried = claim_next_publication(
        database_path=database_path,
        now_utc=NOW + timedelta(hours=2),
    )
    assert retried is not None and retried.id == first.id
    assert finish_publication_failure(
        database_path=database_path,
        publication=retried,
        error="forbidden",
        permanent=True,
        now_utc=NOW + timedelta(hours=2),
    )
    second = claim_next_publication(
        database_path=database_path,
        now_utc=NOW + timedelta(hours=2),
    )
    assert second is not None and second.id != first.id


def test_missing_message_is_replaced(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"
    contest_id = _seed_publications(database_path)
    _deliver_and_finish(database_path, RecordingBot(), ("old",))
    claim = _revise_and_claim(database_path, contest_id)

    class MissingMessageBot(RecordingBot):
        async def edit_message_text(self, *args, **kwargs) -> bool:
            raise TelegramBadRequest(
                method=SendMessage(chat_id=-1001, text="x"),
                message="message to edit not found",
            )

    bot = MissingMessageBot()
    asyncio.run(
        deliver_publication(
            bot=bot,
            database_path=database_path,
            publication=claim,
            desired_messages=("replacement",),
        )
    )
    assert len(bot.sent) == 1
    with database_connection(database_path) as connection:
        message_id = connection.execute(
            "SELECT telegram_message_id FROM contest_publication_messages"
        ).fetchone()[0]
    assert message_id == 1000


def test_multipart_shrink_deletes_extra_part(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"
    contest_id = _seed_publications(database_path)
    first_bot = RecordingBot()
    _deliver_and_finish(database_path, first_bot, ("first", "second"))
    claim = _revise_and_claim(database_path, contest_id)
    bot = RecordingBot()
    asyncio.run(
        deliver_publication(
            bot=bot,
            database_path=database_path,
            publication=claim,
            desired_messages=("first",),
        )
    )
    assert bot.deleted == [{"chat_id": -1001, "message_id": 1001}]
    with database_connection(database_path) as connection:
        parts = connection.execute(
            "SELECT part_number FROM contest_publication_messages"
        ).fetchall()
    assert [row[0] for row in parts] == [0]


def test_delete_failure_uses_fallback_neutralization(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"
    contest_id = _seed_publications(database_path)
    _deliver_and_finish(database_path, RecordingBot(), ("first", "second"))
    claim = _revise_and_claim(database_path, contest_id)

    class NeutralizingBot(RecordingBot):
        async def delete_message(self, chat_id: int, message_id: int) -> bool:
            raise TelegramBadRequest(
                method=SendMessage(chat_id=chat_id, text="x"),
                message="message cannot be deleted",
            )

    bot = NeutralizingBot()
    asyncio.run(
        deliver_publication(
            bot=bot,
            database_path=database_path,
            publication=claim,
            desired_messages=("first",),
        )
    )
    assert len(bot.edited) == 1
    with database_connection(database_path) as connection:
        status = connection.execute(
            """
            SELECT part_status FROM contest_publication_messages
            WHERE part_number = 1
            """
        ).fetchone()[0]
    assert status == "retired"


def test_permanent_part_failure_persists_reason_and_releases_delivery(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    contest_id = _seed_publications(database_path)
    _deliver_and_finish(database_path, RecordingBot(), ("first", "second"))
    claim = _revise_and_claim(database_path, contest_id)

    class ForbiddenDeleteBot(RecordingBot):
        async def delete_message(self, chat_id: int, message_id: int) -> bool:
            raise TelegramForbiddenError(
                method=SendMessage(chat_id=chat_id, text="x"),
                message="bot was blocked",
            )

    asyncio.run(
        deliver_publication(
            bot=ForbiddenDeleteBot(),
            database_path=database_path,
            publication=claim,
            desired_messages=("first",),
        )
    )
    assert finish_publication_success(
        database_path=database_path,
        publication=claim,
        status="published",
    )
    with database_connection(database_path) as connection:
        row = connection.execute(
            """
            SELECT part_status, last_error
            FROM contest_publication_messages
            WHERE part_number = 1
            """
        ).fetchone()
    assert row["part_status"] == "terminal_failed"
    assert "bot was blocked" in row["last_error"]


def test_retry_horizon_stays_pending_until_boundary(tmp_path: Path) -> None:
    retry_horizon = sum(
        min(PUBLICATION_MAX_BACKOFF_SECONDS, 2 ** max(0, attempt - 1))
        for attempt in range(1, PUBLICATION_MAX_ATTEMPTS)
    )
    assert retry_horizon >= 6 * 3600
    database_path = tmp_path / "predictor.db"
    _seed_publications(database_path)
    with database_connection(database_path) as connection:
        connection.execute(
            "UPDATE contest_publications SET attempt_count = ?",
            (PUBLICATION_MAX_ATTEMPTS - 2,),
        )
    claim = claim_next_publication(database_path=database_path, now_utc=NOW)
    assert claim is not None
    assert finish_publication_failure(
        database_path=database_path,
        publication=claim,
        error="network",
        permanent=False,
        retry_after_seconds=0,
        now_utc=NOW,
    )
    with database_connection(database_path) as connection:
        before = connection.execute(
            "SELECT delivery_status, attempt_count FROM contest_publications"
        ).fetchone()
    assert tuple(before) == ("pending", PUBLICATION_MAX_ATTEMPTS - 1)

    final_claim = claim_next_publication(database_path=database_path, now_utc=NOW)
    assert final_claim is not None
    assert finish_publication_failure(
        database_path=database_path,
        publication=final_claim,
        error="network",
        permanent=False,
        now_utc=NOW,
    )
    with database_connection(database_path) as connection:
        after = connection.execute(
            """
            SELECT delivery_status, attempt_count, settled_revision
            FROM contest_publications
            """
        ).fetchone()
    assert tuple(after) == ("terminal_failed", PUBLICATION_MAX_ATTEMPTS, 1)


@pytest.mark.parametrize(
    ("enabled", "publication_type", "created"),
    [
        (False, "match_result", False),
        (False, "champion_result", False),
        (False, "contest_completed", False),
        (True, "match_result", True),
        (True, "champion_result", True),
        (True, "contest_completed", True),
    ],
)
def test_master_publication_switch_matrix(
    tmp_path: Path,
    enabled: bool,
    publication_type: str,
    created: bool,
) -> None:
    database_path = tmp_path / f"{enabled}-{publication_type}.db"
    initialize_database(database_path)
    with database_connection(database_path) as connection:
        chat_id = connection.execute(
            "INSERT INTO chats (telegram_chat_id, title) VALUES (-1001, 'chat')"
        ).lastrowid
        contest_id = connection.execute(
            """
            INSERT INTO contests (
                chat_id, name, slug, match_prediction_publication_enabled
            ) VALUES (?, 'contest', 'contest', ?)
            """,
            (chat_id, int(enabled)),
        ).lastrowid
        assert contest_id is not None
        assert (
            create_publication_if_enabled(
                connection,
                contest_id=int(contest_id),
                publication_type=publication_type,  # type: ignore[arg-type]
                entity_id=1,
                event_id=1,
                now_utc=NOW,
            )
            is created
        )


def _seed_publications(database_path: Path, *, count: int = 1) -> int:
    initialize_database(database_path)
    now_value = serialize_service_time(NOW)
    with database_connection(database_path) as connection:
        chat_id = connection.execute(
            "INSERT INTO chats (telegram_chat_id, title) VALUES (-1001, 'chat')"
        ).lastrowid
        contest_id = connection.execute(
            """
            INSERT INTO contests (
                chat_id, name, slug, match_prediction_publication_enabled
            ) VALUES (?, 'contest', 'contest', 1)
            """,
            (chat_id,),
        ).lastrowid
        assert contest_id is not None
        for index in range(count):
            connection.execute(
                """
                INSERT INTO contest_publications (
                    contest_id, publication_type, entity_id,
                    first_event_id, latest_event_id, next_attempt_at,
                    created_at, updated_at
                ) VALUES (?, 'contest_completed', ?, ?, ?, ?, ?, ?)
                """,
                (
                    contest_id,
                    index + 1,
                    index + 1,
                    index + 1,
                    now_value,
                    now_value,
                    now_value,
                ),
            )
    return int(contest_id)


def _release_claim(database_path: Path, publication_id: int) -> None:
    with database_connection(database_path) as connection:
        connection.execute(
            """
            UPDATE contest_publications
            SET claim_token = NULL, claim_expires_at = NULL
            WHERE id = ?
            """,
            (publication_id,),
        )


def _deliver_and_finish(
    database_path: Path,
    bot: RecordingBot,
    messages: tuple[str, ...],
) -> None:
    claim = claim_next_publication(database_path=database_path)
    assert claim is not None
    asyncio.run(
        deliver_publication(
            bot=bot,
            database_path=database_path,
            publication=claim,
            desired_messages=messages,
        )
    )
    assert finish_publication_success(
        database_path=database_path,
        publication=claim,
        status="published",
    )


def _revise_and_claim(database_path: Path, contest_id: int):
    with database_connection(database_path) as connection:
        assert revise_existing_publication(
            connection,
            contest_id=contest_id,
            publication_type="contest_completed",
            entity_id=1,
            event_id=100,
            desired_action="publish",
            now_utc=NOW,
        )
    claim = claim_next_publication(database_path=database_path)
    assert claim is not None
    return claim
