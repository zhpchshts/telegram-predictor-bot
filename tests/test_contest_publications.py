from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest
from aiogram.types import InputRichMessage

from app import contest_publications
from app.audit_service import AuditActor, AuditActorRole
from app.contest_service import (
    ChampionPredictionSettingsLockedError,
    ContestCompletionUnavailableError,
    LeaderboardTiebreakReason,
    complete_contest,
    create_match,
    create_world_cup_2026_contest,
    delete_match,
    get_contest_details,
    save_champion_prediction,
    save_champion_prediction_settings,
    save_contest_champion,
    save_match_prediction,
    save_match_prediction_publication_settings,
    save_match_result,
)
from app.database import database_connection, initialize_database
from app.contest_publications import render_publication_messages
from app.publication_delivery import TemporaryDeliveryError, deliver_publication
from app.publication_outbox import (
    ClaimedPublication,
    StalePublicationRevision,
    claim_next_publication,
    create_or_revise_champion_publication,
    create_or_revise_champion_predictions_publication,
    finish_publication_failure,
    finish_publication_success,
    prepare_scheduled_reconciliation,
    restore_legacy_champion_result_reconciliations,
    revise_existing_publication,
    serialize_service_time,
)
from app.publication_worker import process_due_contest_publications


CHAT_ID = -1001234567890
USER_ID = 123456789
AUDIT_ACTOR = AuditActor(
    telegram_chat_id=CHAT_ID,
    telegram_user_id=USER_ID,
    role=AuditActorRole.TELEGRAM_ADMIN,
)


@dataclass(frozen=True, slots=True)
class SentMessage:
    message_id: int


class RecordingBot:
    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []
        self.edited: list[dict[str, object]] = []
        self.deleted: list[dict[str, int]] = []

    async def send_rich_message(
        self, chat_id: int, *, rich_message: InputRichMessage
    ) -> SentMessage:
        message_id = 1000 + len(self.sent)
        self.sent.append(
            {
                "chat_id": chat_id,
                "text": rich_message.html,
                "rich_message": rich_message,
            }
        )
        return SentMessage(message_id=message_id)

    async def edit_message_text(
        self,
        *,
        chat_id: int,
        message_id: int,
        rich_message: InputRichMessage,
    ) -> bool:
        self.edited.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": rich_message.html,
                "rich_message": rich_message,
            }
        )
        return True

    async def delete_message(self, chat_id: int, message_id: int) -> bool:
        self.deleted.append({"chat_id": chat_id, "message_id": message_id})
        return True


class FailingBot(RecordingBot):
    def __init__(self, *, fail_on_send: int) -> None:
        super().__init__()
        self.fail_on_send = fail_on_send
        self.send_attempts = 0

    async def send_rich_message(
        self, chat_id: int, *, rich_message: InputRichMessage
    ) -> SentMessage:
        self.send_attempts += 1
        if self.send_attempts == self.fail_on_send:
            raise RuntimeError("Telegram is temporarily unavailable.")
        return await super().send_rich_message(chat_id, rich_message=rich_message)


def test_publication_schema_is_additive_idempotent_and_empty(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    initialize_database(database_path)

    with database_connection(database_path) as connection:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        publication_count = connection.execute(
            "SELECT COUNT(*) FROM contest_publications"
        ).fetchone()[0]
    assert "contest_publications" in tables
    assert "contest_publication_messages" in tables
    assert publication_count == 0


def test_publication_schema_rebuild_preserves_rows_and_foreign_keys(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
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
        connection.execute(
            """
            INSERT INTO contest_publications (
                id, contest_id, publication_type, entity_id,
                desired_revision, settled_revision, desired_action,
                delivery_status, first_event_id, latest_event_id,
                reconcile_at, claim_token, claim_expires_at, attempt_count,
                next_attempt_at, last_error, created_at, updated_at
            ) VALUES (
                77, ?, 'champion_result', ?, 5, 3, 'publish', 'pending',
                41, 42, '2026-07-21T10:00:00.000000Z', 'claim-77',
                '2026-07-21T10:01:30.000000Z', 4,
                '2026-07-21T10:02:00.000000Z', 'temporary failure',
                '2026-07-21T09:00:00.000000Z',
                '2026-07-21T09:30:00.000000Z'
            )
            """,
            (contest_id, contest_id),
        )
        connection.execute(
            """
            INSERT INTO contest_publication_messages (
                publication_id, part_number, telegram_message_id,
                content_hash, content_text, part_status, last_error,
                sent_at, updated_at
            ) VALUES (
                77, 2, 9001, 'hash', '<p>saved content</p>', 'active', NULL,
                '2026-07-21T09:05:00.000000Z',
                '2026-07-21T09:06:00.000000Z'
            )
            """
        )

    connection = sqlite3.connect(database_path)
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            CREATE TABLE contest_publications_old (
                id INTEGER PRIMARY KEY,
                contest_id INTEGER NOT NULL REFERENCES contests(id) ON DELETE CASCADE,
                publication_type TEXT NOT NULL CHECK (
                    publication_type IN (
                        'match_result', 'champion_result', 'contest_completed'
                    )
                ),
                entity_id INTEGER NOT NULL,
                desired_revision INTEGER NOT NULL DEFAULT 1 CHECK (
                    desired_revision >= 1
                ),
                settled_revision INTEGER NOT NULL DEFAULT 0 CHECK (
                    settled_revision >= 0 AND settled_revision <= desired_revision
                ),
                desired_action TEXT NOT NULL DEFAULT 'publish' CHECK (
                    desired_action IN ('publish', 'withdraw')
                ),
                delivery_status TEXT NOT NULL DEFAULT 'pending' CHECK (
                    delivery_status IN (
                        'pending', 'published', 'withdrawn', 'terminal_failed'
                    )
                ),
                first_event_id INTEGER NOT NULL,
                latest_event_id INTEGER NOT NULL,
                reconcile_at TEXT,
                claim_token TEXT,
                claim_expires_at TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
                next_attempt_at TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (contest_id, publication_type, entity_id),
                CHECK (
                    (claim_token IS NULL AND claim_expires_at IS NULL)
                    OR (claim_token IS NOT NULL AND claim_expires_at IS NOT NULL)
                )
            )
            """
        )
        connection.execute(
            """
            INSERT INTO contest_publications_old
            SELECT * FROM contest_publications
            """
        )
        connection.execute("DROP TABLE contest_publications")
        connection.execute(
            "ALTER TABLE contest_publications_old RENAME TO contest_publications"
        )
        connection.execute("COMMIT")
        connection.execute("PRAGMA foreign_keys = ON")
    finally:
        connection.close()

    for _ in range(2):
        initialize_database(database_path)
        with database_connection(database_path) as connection:
            publication = connection.execute(
                "SELECT * FROM contest_publications WHERE id = 77"
            ).fetchone()
            message = connection.execute(
                """
                SELECT * FROM contest_publication_messages
                WHERE publication_id = 77 AND part_number = 2
                """
            ).fetchone()
            assert publication is not None
            assert tuple(publication) == (
                77,
                contest_id,
                "champion_result",
                contest_id,
                5,
                3,
                "publish",
                "pending",
                41,
                42,
                "2026-07-21T10:00:00.000000Z",
                "claim-77",
                "2026-07-21T10:01:30.000000Z",
                4,
                "2026-07-21T10:02:00.000000Z",
                "temporary failure",
                "2026-07-21T09:00:00.000000Z",
                "2026-07-21T09:30:00.000000Z",
            )
            assert message is not None
            assert int(message["publication_id"]) == 77
            assert message["content_text"] == "<p>saved content</p>"
            assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    with database_connection(database_path) as connection:
        connection.execute(
            """
            INSERT INTO contest_publications (
                contest_id, publication_type, entity_id,
                first_event_id, latest_event_id, created_at, updated_at
            ) VALUES (
                ?, 'champion_predictions', ?, 50, 50,
                '2026-07-21T10:00:00.000000Z',
                '2026-07-21T10:00:00.000000Z'
            )
            """,
            (contest_id, contest_id),
        )


def test_champion_predictions_follow_deadline_and_render_current_sorted_list(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    contest_id, match = _create_contest_and_match(database_path)
    _enable_publications(database_path, contest_id=contest_id)
    save_champion_prediction_settings(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=USER_ID,
        first_name="Анна <&>",
        last_name="Иванова",
        username="anna",
        enabled=True,
        deadline_at="2026-06-11T11:00:00Z",
        points=7,
        now_utc=_datetime(9),
        audit_actor=AUDIT_ACTOR,
    )
    for telegram_user_id, first_name, team_id in (
        (USER_ID + 1, "Борис", match.home_team_id),
        (USER_ID, "Анна <&>", match.home_team_id),
        (USER_ID, "Анна <&>", match.away_team_id),
    ):
        save_champion_prediction(
            database_path=database_path,
            telegram_chat_id=CHAT_ID,
            contest_id=contest_id,
            telegram_user_id=telegram_user_id,
            first_name=first_name,
            last_name="Иванова",
            username=f"user-{telegram_user_id}",
            predicted_team_id=team_id,
            now_utc=_datetime(10),
        )

    with database_connection(database_path) as connection:
        row = connection.execute(
            """
            SELECT desired_revision, desired_action, reconcile_at
            FROM contest_publications
            WHERE contest_id = ? AND publication_type = 'champion_predictions'
            """,
            (contest_id,),
        ).fetchone()
    assert tuple(row) == (
        1,
        "withdraw",
        "2026-06-11T11:00:00.000000Z",
    )

    before_deadline = claim_next_publication(
        database_path=database_path,
        now_utc=_datetime(10),
    )
    assert before_deadline is not None
    assert before_deadline.publication_type == "champion_predictions"
    prepared = prepare_scheduled_reconciliation(
        database_path=database_path,
        publication=before_deadline,
        now_utc=_datetime(10),
    )
    assert prepared is not None and prepared.desired_action == "withdraw"
    bot = RecordingBot()
    asyncio.run(
        deliver_publication(
            bot=bot,
            database_path=database_path,
            publication=prepared,
            desired_messages=(),
        )
    )
    assert finish_publication_success(
        database_path=database_path,
        publication=prepared,
        status="withdrawn",
        now_utc=_datetime(10),
    )
    assert bot.sent == []

    at_deadline = claim_next_publication(
        database_path=database_path,
        now_utc=_datetime(12),
    )
    assert at_deadline is not None
    prepared = prepare_scheduled_reconciliation(
        database_path=database_path,
        publication=at_deadline,
        now_utc=_datetime(12),
    )
    assert prepared is not None
    assert prepared.desired_action == "publish"
    messages = render_publication_messages(
        database_path=database_path,
        publication=prepared,
        max_message_length=130,
        now_utc=_datetime(12),
    )
    assert len(messages) == 2
    text = "".join(messages)
    assert "🏆 <b>Прогнозы на чемпиона</b>" in text
    assert "Анна &lt;&amp;&gt; Иванова" in text
    assert text.index("Анна") < text.index("Борис")
    assert "Чемпион турнира" not in text
    assert "Очки" not in text
    with database_connection(database_path) as connection:
        away_name = connection.execute(
            "SELECT name FROM teams WHERE id = ?", (match.away_team_id,)
        ).fetchone()[0]
    assert str(away_name) in text


def test_champion_predictions_renderer_has_empty_state(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"
    contest_id, _ = _create_contest_and_match(database_path)
    _enable_publications(database_path, contest_id=contest_id)
    save_champion_prediction_settings(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=USER_ID,
        first_name="Анна",
        last_name="Иванова",
        username="anna",
        enabled=True,
        deadline_at="2026-06-11T11:00:00Z",
        points=5,
        now_utc=_datetime(12),
        audit_actor=AUDIT_ACTOR,
    )
    claim = claim_next_publication(
        database_path=database_path,
        now_utc=_datetime(12),
    )
    assert claim is not None and claim.publication_type == "champion_predictions"
    assert render_publication_messages(
        database_path=database_path,
        publication=claim,
        now_utc=_datetime(12),
    ) == (
        "<p>🏆 <b>Прогнозы на чемпиона</b></p>"
        "<p>Конкурс: «ЧМ-2026»</p>"
        "<p>Никто не сделал прогноз на чемпиона.</p>",
    )


def test_champion_predictions_future_deadline_move_does_not_add_revision(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    contest_id, _ = _create_contest_and_match(database_path)
    _enable_publications(database_path, contest_id=contest_id)
    for deadline in (
        "2099-01-01T00:00:00Z",
        "2099-02-01T00:00:00Z",
    ):
        save_champion_prediction_settings(
            database_path=database_path,
            telegram_chat_id=CHAT_ID,
            contest_id=contest_id,
            telegram_user_id=USER_ID,
            first_name="Анна",
            last_name="Иванова",
            username="anna",
            enabled=True,
            deadline_at=deadline,
            points=5,
            now_utc=_datetime(9),
            audit_actor=AUDIT_ACTOR,
        )
    with database_connection(database_path) as connection:
        row = connection.execute(
            """
            SELECT desired_revision, desired_action, reconcile_at
            FROM contest_publications
            WHERE contest_id = ? AND publication_type = 'champion_predictions'
            """,
            (contest_id,),
        ).fetchone()
    assert tuple(row) == (
        1,
        "withdraw",
        "2099-02-01T00:00:00.000000Z",
    )


def test_master_switch_after_deadline_has_no_backfill_but_explicit_save_does(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    contest_id, _ = _create_contest_and_match(database_path)
    save_champion_prediction_settings(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=USER_ID,
        first_name="Анна",
        last_name="Иванова",
        username="anna",
        enabled=True,
        deadline_at="2026-06-11T11:00:00Z",
        points=5,
        now_utc=_datetime(12),
        audit_actor=AUDIT_ACTOR,
    )
    save_match_prediction_publication_settings(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=USER_ID,
        first_name="Анна",
        last_name="Иванова",
        username="anna",
        enabled=True,
        now_utc=_datetime(12),
        audit_actor=AUDIT_ACTOR,
    )
    with database_connection(database_path) as connection:
        assert (
            connection.execute(
                """
            SELECT COUNT(*) FROM contest_publications
            WHERE publication_type = 'champion_predictions'
            """
            ).fetchone()[0]
            == 0
        )

    save_champion_prediction_settings(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=USER_ID,
        first_name="Анна",
        last_name="Иванова",
        username="anna",
        enabled=True,
        deadline_at="2026-06-11T11:00:00Z",
        points=5,
        now_utc=_datetime(12),
        audit_actor=AUDIT_ACTOR,
    )
    with database_connection(database_path) as connection:
        row = connection.execute(
            """
            SELECT desired_action, reconcile_at
            FROM contest_publications
            WHERE publication_type = 'champion_predictions'
            """
        ).fetchone()
    assert tuple(row) == ("publish", None)


def test_master_switch_disable_and_reenable_restores_future_reconciliation(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    contest_id, _ = _create_contest_and_match(database_path)
    _enable_publications(database_path, contest_id=contest_id)
    save_champion_prediction_settings(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=USER_ID,
        first_name="Анна",
        last_name="Иванова",
        username="anna",
        enabled=True,
        deadline_at="2099-01-01T00:00:00Z",
        points=5,
        now_utc=_datetime(9),
        audit_actor=AUDIT_ACTOR,
    )
    _disable_publications(database_path, contest_id=contest_id)
    with database_connection(database_path) as connection:
        disabled = connection.execute(
            """
            SELECT desired_revision, settled_revision, reconcile_at
            FROM contest_publications
            WHERE publication_type = 'champion_predictions'
            """
        ).fetchone()
    assert tuple(disabled) == (2, 2, None)

    _enable_publications(database_path, contest_id=contest_id)
    with database_connection(database_path) as connection:
        enabled = connection.execute(
            """
            SELECT desired_revision, settled_revision, desired_action, reconcile_at
            FROM contest_publications
            WHERE publication_type = 'champion_predictions'
            """
        ).fetchone()
    assert tuple(enabled) == (
        2,
        2,
        "withdraw",
        "2099-01-01T00:00:00.000000Z",
    )


def test_reopened_champion_predictions_are_withdrawn_and_republished(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    contest_id, match = _create_contest_and_match(database_path)
    _enable_publications(database_path, contest_id=contest_id)
    save_champion_prediction_settings(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=USER_ID,
        first_name="Анна",
        last_name="Иванова",
        username="anna",
        enabled=True,
        deadline_at="2026-06-11T11:00:00Z",
        points=5,
        now_utc=_datetime(9),
        audit_actor=AUDIT_ACTOR,
    )
    save_champion_prediction(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=USER_ID,
        first_name="Анна",
        last_name="Иванова",
        username="anna",
        predicted_team_id=match.home_team_id,
        now_utc=_datetime(10),
    )
    save_champion_prediction_settings(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=USER_ID,
        first_name="Анна",
        last_name="Иванова",
        username="anna",
        enabled=True,
        deadline_at="2026-06-11T11:00:00Z",
        points=5,
        now_utc=_datetime(12),
        audit_actor=AUDIT_ACTOR,
    )
    bot = RecordingBot()
    claim = claim_next_publication(database_path=database_path, now_utc=_datetime(12))
    assert claim is not None and claim.publication_type == "champion_predictions"
    messages = render_publication_messages(
        database_path=database_path,
        publication=claim,
        now_utc=_datetime(12),
    )
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
        now_utc=_datetime(12),
    )

    future_deadline = "2099-01-01T00:00:00Z"
    save_champion_prediction_settings(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=USER_ID,
        first_name="Анна",
        last_name="Иванова",
        username="anna",
        enabled=True,
        deadline_at=future_deadline,
        points=5,
        now_utc=_datetime(12),
        audit_actor=AUDIT_ACTOR,
    )
    withdrawal = claim_next_publication(
        database_path=database_path, now_utc=_datetime(12)
    )
    assert withdrawal is not None and withdrawal.desired_action == "withdraw"
    asyncio.run(
        deliver_publication(
            bot=bot,
            database_path=database_path,
            publication=withdrawal,
            desired_messages=(),
        )
    )
    assert finish_publication_success(
        database_path=database_path,
        publication=withdrawal,
        status="withdrawn",
        now_utc=_datetime(12),
    )
    assert bot.deleted == [{"chat_id": CHAT_ID, "message_id": 1000}]

    save_champion_prediction(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=USER_ID,
        first_name="Анна",
        last_name="Иванова",
        username="anna",
        predicted_team_id=match.away_team_id,
        now_utc=datetime(2098, 12, 31, 12, tzinfo=timezone.utc),
    )
    due = claim_next_publication(
        database_path=database_path,
        now_utc=datetime(2099, 1, 1, 1, tzinfo=timezone.utc),
    )
    assert due is not None
    republish = prepare_scheduled_reconciliation(
        database_path=database_path,
        publication=due,
        now_utc=datetime(2099, 1, 1, 1, tzinfo=timezone.utc),
    )
    assert republish is not None and republish.desired_action == "publish"
    republished_messages = render_publication_messages(
        database_path=database_path,
        publication=republish,
        now_utc=datetime(2099, 1, 1, 1, tzinfo=timezone.utc),
    )
    with database_connection(database_path) as connection:
        away_name = connection.execute(
            "SELECT name FROM teams WHERE id = ?", (match.away_team_id,)
        ).fetchone()[0]
    assert str(away_name) in "".join(republished_messages)

    assert finish_publication_success(
        database_path=database_path,
        publication=republish,
        status="published",
        now_utc=datetime(2099, 1, 1, 1, tzinfo=timezone.utc),
    )
    save_champion_prediction_settings(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=USER_ID,
        first_name="Анна",
        last_name="Иванова",
        username="anna",
        enabled=False,
        deadline_at=None,
        points=5,
        now_utc=datetime(2099, 1, 1, 2, tzinfo=timezone.utc),
        audit_actor=AUDIT_ACTOR,
    )
    with database_connection(database_path) as connection:
        row = connection.execute(
            """
            SELECT desired_action, reconcile_at
            FROM contest_publications
            WHERE publication_type = 'champion_predictions'
            """
        ).fetchone()
    assert tuple(row) == ("withdraw", None)


def test_recording_actual_champion_does_not_revise_champion_predictions(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    contest_id, match = _create_contest_and_match(database_path)
    _enable_publications(database_path, contest_id=contest_id)
    save_champion_prediction_settings(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=USER_ID,
        first_name="Анна",
        last_name="Иванова",
        username="anna",
        enabled=True,
        deadline_at="2026-06-11T11:00:00Z",
        points=5,
        now_utc=_datetime(12),
        audit_actor=AUDIT_ACTOR,
    )
    _save_result(database_path, contest_id=contest_id, match=match)
    with database_connection(database_path) as connection:
        before = tuple(
            connection.execute(
                """
                SELECT desired_revision, latest_event_id, updated_at
                FROM contest_publications
                WHERE publication_type = 'champion_predictions'
                """
            ).fetchone()
        )
    save_contest_champion(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=USER_ID,
        first_name="Анна",
        last_name="Иванова",
        username="anna",
        champion_team_id=match.home_team_id,
        audit_actor=AUDIT_ACTOR,
    )
    with database_connection(database_path) as connection:
        after = tuple(
            connection.execute(
                """
                SELECT desired_revision, latest_event_id, updated_at
                FROM contest_publications
                WHERE publication_type = 'champion_predictions'
                """
            ).fetchone()
        )
    assert after == before


def test_match_result_publication_is_sent_and_corrected(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"
    contest_id, match = _create_contest_and_match(database_path)
    _enable_publications(database_path, contest_id=contest_id)
    save_match_prediction(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        match_id=match.id,
        telegram_user_id=USER_ID,
        first_name="Анна <&>",
        last_name="Иванова",
        username="anna",
        predicted_home_score=2,
        predicted_away_score=1,
        predicted_advancing_team_id=match.home_team_id,
        now_utc=_datetime(10),
    )
    save_match_result(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        match_id=match.id,
        telegram_user_id=USER_ID,
        first_name="Анна <&>",
        last_name="Иванова",
        username="anna",
        home_score=2,
        away_score=1,
        advancing_team_id=match.home_team_id,
        now_utc=_datetime(12),
        audit_actor=AUDIT_ACTOR,
    )

    bot = RecordingBot()
    assert (
        asyncio.run(
            process_due_contest_publications(
                bot=bot,
                database_path=database_path,
            )
        )
        == 1
    )
    assert len(bot.sent) == 1
    rich_message = bot.sent[0]["rich_message"]
    assert isinstance(rich_message, InputRichMessage)
    assert rich_message.skip_entity_detection is True
    assert "2:1" in str(bot.sent[0]["text"])
    assert "<table bordered striped>" in str(bot.sent[0]["text"])
    assert '<th colspan="4" align="left">Участник</th>' in str(bot.sent[0]["text"])
    assert '<th colspan="3" align="center">Прогноз</th>' in str(bot.sent[0]["text"])
    assert '<th align="right">Очки</th>' in str(bot.sent[0]["text"])
    assert '<td align="right"><b>+4</b></td>' in str(bot.sent[0]["text"])
    assert "Анна &lt;&amp;&gt; Иванова" in str(bot.sent[0]["text"])

    save_match_result(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        match_id=match.id,
        telegram_user_id=USER_ID,
        first_name="Анна <&>",
        last_name="Иванова",
        username="anna",
        home_score=1,
        away_score=0,
        advancing_team_id=match.home_team_id,
        now_utc=_datetime(13),
        audit_actor=AUDIT_ACTOR,
    )
    assert (
        asyncio.run(
            process_due_contest_publications(
                bot=bot,
                database_path=database_path,
            )
        )
        == 1
    )
    assert len(bot.sent) == 1
    assert len(bot.edited) == 1
    assert "1:0" in str(bot.edited[0]["text"])

    save_match_result(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        match_id=match.id,
        telegram_user_id=USER_ID,
        first_name="Анна <&>",
        last_name="Иванова",
        username="anna",
        home_score=1,
        away_score=1,
        advancing_team_id=match.home_team_id,
        now_utc=_datetime(14),
        audit_actor=AUDIT_ACTOR,
    )
    assert (
        asyncio.run(
            process_due_contest_publications(
                bot=bot,
                database_path=database_path,
            )
        )
        == 1
    )
    assert len(bot.edited) == 2
    assert "В следующий раунд проходит Франция" in str(bot.edited[-1]["text"])


def test_disabled_first_result_is_not_backfilled_by_correction(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    contest_id, match = _create_contest_and_match(database_path)
    _save_result(database_path, contest_id=contest_id, match=match)
    _enable_publications(database_path, contest_id=contest_id)
    _save_result(
        database_path,
        contest_id=contest_id,
        match=match,
        home_score=3,
        away_score=1,
    )

    with database_connection(database_path) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM contest_publications"
        ).fetchone()[0]
    assert count == 0


def test_delete_pending_match_publication_removes_it(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"
    contest_id, match = _create_contest_and_match(database_path)
    _enable_publications(database_path, contest_id=contest_id)
    _save_result(database_path, contest_id=contest_id, match=match)

    _delete_match(database_path, contest_id=contest_id, match_id=match.id)

    with database_connection(database_path) as connection:
        publication_count = connection.execute(
            "SELECT COUNT(*) FROM contest_publications"
        ).fetchone()[0]
        match_count = connection.execute(
            "SELECT COUNT(*) FROM matches WHERE id = ?", (match.id,)
        ).fetchone()[0]
    assert publication_count == 0
    assert match_count == 0


def test_delete_claimed_match_publication_creates_tombstone(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    contest_id, match = _create_contest_and_match(database_path)
    _enable_publications(database_path, contest_id=contest_id)
    _save_result(database_path, contest_id=contest_id, match=match)
    claimed = claim_next_publication(database_path=database_path)
    assert claimed is not None

    _delete_match(database_path, contest_id=contest_id, match_id=match.id)

    with database_connection(database_path) as connection:
        publication = connection.execute(
            """
            SELECT entity_id, desired_action, desired_revision, claim_token
            FROM contest_publications
            WHERE contest_id = ? AND publication_type = 'match_result'
            """,
            (contest_id,),
        ).fetchone()
    assert publication is not None
    assert publication["entity_id"] < 0
    assert publication["desired_action"] == "withdraw"
    assert publication["desired_revision"] == 2
    assert publication["claim_token"] == claimed.claim_token


def test_deleted_published_match_id_can_be_reused_in_same_contest(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    first_contest_id, first_match = _create_contest_and_match(database_path)
    _enable_publications(database_path, contest_id=first_contest_id)
    _save_result(database_path, contest_id=first_contest_id, match=first_match)
    bot = RecordingBot()
    assert (
        asyncio.run(
            process_due_contest_publications(bot=bot, database_path=database_path)
        )
        == 1
    )
    _delete_match(
        database_path,
        contest_id=first_contest_id,
        match_id=first_match.id,
    )
    assert (
        asyncio.run(
            process_due_contest_publications(bot=bot, database_path=database_path)
        )
        == 1
    )

    second_match = create_match(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=first_contest_id,
        telegram_user_id=USER_ID,
        first_name="Анна",
        last_name="Иванова",
        username="anna",
        home_team_name="Бразилия",
        away_team_name="Аргентина",
        starts_at_utc="2026-06-11T12:00:00Z",
        idempotency_key="match-2",
        audit_actor=AUDIT_ACTOR,
    ).match
    assert second_match.id == first_match.id
    _save_result(
        database_path,
        contest_id=first_contest_id,
        match=second_match,
    )

    with database_connection(database_path) as connection:
        publications = connection.execute(
            """
            SELECT contest_id, entity_id, desired_action
            FROM contest_publications
            WHERE publication_type = 'match_result'
            ORDER BY id
            """
        ).fetchall()
    assert [tuple(row) for row in publications] == [
        (first_contest_id, -1, "withdraw"),
        (first_contest_id, second_match.id, "publish"),
    ]
    assert (
        asyncio.run(
            process_due_contest_publications(bot=bot, database_path=database_path)
        )
        == 1
    )
    assert "Бразилия" in str(bot.sent[-1]["text"])


def test_match_deletion_during_send_is_reconciled_without_untracked_message(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    contest_id, match = _create_contest_and_match(database_path)
    _enable_publications(database_path, contest_id=contest_id)
    _save_result(database_path, contest_id=contest_id, match=match)

    class DeletingBot(RecordingBot):
        async def send_rich_message(
            self, chat_id: int, *, rich_message: InputRichMessage
        ) -> SentMessage:
            sent = await super().send_rich_message(chat_id, rich_message=rich_message)
            _delete_match(database_path, contest_id=contest_id, match_id=match.id)
            return sent

    bot = DeletingBot()
    assert (
        asyncio.run(
            process_due_contest_publications(
                bot=bot,
                database_path=database_path,
                max_publications=1,
            )
        )
        == 0
    )
    with database_connection(database_path) as connection:
        pending = connection.execute(
            """
            SELECT entity_id, desired_action, delivery_status,
                   desired_revision, settled_revision
            FROM contest_publications
            """
        ).fetchone()
    assert tuple(pending) == (-1, "withdraw", "pending", 2, 1)
    assert bot.deleted == [{"chat_id": CHAT_ID, "message_id": 1000}]
    assert (
        asyncio.run(
            process_due_contest_publications(
                bot=bot,
                database_path=database_path,
                max_publications=1,
            )
        )
        == 1
    )
    assert bot.deleted == [{"chat_id": CHAT_ID, "message_id": 1000}]


def test_final_actions_are_published_in_domain_order(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"
    contest_id, match = _create_contest_and_match(database_path)
    _enable_publications(database_path, contest_id=contest_id)
    save_champion_prediction_settings(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=USER_ID,
        first_name="Анна",
        last_name="Иванова",
        username="anna",
        enabled=True,
        deadline_at="2026-06-11T11:00:00Z",
        points=5,
        audit_actor=AUDIT_ACTOR,
    )
    save_champion_prediction(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=USER_ID,
        first_name="Анна",
        last_name="Иванова",
        username="anna",
        predicted_team_id=match.home_team_id,
        now_utc=_datetime(10),
    )
    _save_result(database_path, contest_id=contest_id, match=match)
    save_contest_champion(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=USER_ID,
        first_name="Анна",
        last_name="Иванова",
        username="anna",
        champion_team_id=match.home_team_id,
        audit_actor=AUDIT_ACTOR,
    )
    complete_contest(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=USER_ID,
        first_name="Анна",
        last_name="Иванова",
        username="anna",
        audit_actor=AUDIT_ACTOR,
    )

    bot = RecordingBot()
    assert (
        asyncio.run(
            process_due_contest_publications(
                bot=bot,
                database_path=database_path,
            )
        )
        == 4
    )
    assert len(bot.sent) == 4
    assert "Прогнозы на чемпиона" in str(bot.sent[0]["text"])
    assert "Прогнозов на этот матч не было" in str(bot.sent[1]["text"])
    assert "Чемпион турнира" in str(bot.sent[2]["text"])
    assert '<th colspan="3" align="left">Участник</th>' in str(bot.sent[2]["text"])
    assert '<th colspan="3" align="center">Прогноз</th>' in str(bot.sent[2]["text"])
    assert "Итоговый рейтинг" in str(bot.sent[3]["text"])


def test_completion_publication_names_exactly_one_winner_for_tied_points(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    contest_id, match = _create_contest_and_match(database_path)
    _enable_publications(database_path, contest_id=contest_id)

    for telegram_user_id, first_name, username in (
        (USER_ID, "Анна", "anna"),
        (USER_ID + 1, "Борис", "boris"),
    ):
        save_match_prediction(
            database_path=database_path,
            telegram_chat_id=CHAT_ID,
            contest_id=contest_id,
            match_id=match.id,
            telegram_user_id=telegram_user_id,
            first_name=first_name,
            last_name=None,
            username=username,
            predicted_home_score=2,
            predicted_away_score=1,
            predicted_advancing_team_id=match.home_team_id,
            now_utc=_datetime(10),
        )

    _save_result(database_path, contest_id=contest_id, match=match)
    complete_contest(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=USER_ID,
        first_name="Анна",
        last_name=None,
        username="anna",
        audit_actor=AUDIT_ACTOR,
    )

    details = get_contest_details(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
    )
    winners = [
        entry.participant_name for entry in details.leaderboard if entry.place == 1
    ]
    bot = RecordingBot()
    asyncio.run(
        process_due_contest_publications(
            bot=bot,
            database_path=database_path,
        )
    )
    completion_text = str(bot.sent[-1]["text"])

    assert len(winners) == 1
    assert [entry.place for entry in details.leaderboard] == [1, 2]
    assert f"Победитель — {winners[0]}" in completion_text
    assert "Победители" not in completion_text
    assert completion_text.count(f"Победитель — {winners[0]}") == 1
    assert "Победитель определён жребием." in completion_text
    assert "🥇" in completion_text
    assert "🥈" in completion_text
    assert '<th align="center">Место</th>' in completion_text
    assert '<th colspan="5" align="left">Участник</th>' in completion_text
    assert '<th align="right">Очки</th>' in completion_text


@pytest.mark.parametrize(
    ("reason", "expected_text"),
    (
        ("exact_score", "большему количеству точных счетов"),
        ("goal_difference", "большему количеству угаданных разниц мячей"),
        ("outcome", "большему количеству угаданных исходов"),
        (
            "drawn_advancing_team",
            "большему количеству правильных прогнозов прошедшей команды",
        ),
        ("champion", "правильному прогнозу чемпиона"),
        ("draw", "Победитель определён жребием"),
    ),
)
def test_final_publication_formats_only_decisive_tiebreak_reason(
    reason: LeaderboardTiebreakReason,
    expected_text: str,
) -> None:
    explanation = contest_publications._format_tiebreak_explanation("Анна <&>", reason)

    assert expected_text in explanation
    assert "Анна &lt;&amp;&gt;" in explanation or reason == "draw"


def test_final_publication_rejects_nonconsecutive_places() -> None:
    with pytest.raises(RuntimeError, match="unique consecutive places"):
        contest_publications._validate_leaderboard_invariant(
            (SimpleNamespace(place=1), SimpleNamespace(place=1))
        )


def test_two_workers_do_not_claim_the_same_publication(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"
    contest_id, match = _create_contest_and_match(database_path)
    _enable_publications(database_path, contest_id=contest_id)
    _save_result(database_path, contest_id=contest_id, match=match)

    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = tuple(
            executor.map(
                lambda _: claim_next_publication(database_path=database_path),
                range(2),
            )
        )

    assert sum(claim is not None for claim in claims) == 1


def test_old_delivery_completion_does_not_settle_new_revision(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    contest_id, match = _create_contest_and_match(database_path)
    _enable_publications(database_path, contest_id=contest_id)
    _save_result(database_path, contest_id=contest_id, match=match)
    claimed = claim_next_publication(database_path=database_path)
    assert claimed is not None

    with database_connection(database_path) as connection:
        event_id = connection.execute(
            """
            INSERT INTO event_log (
                contest_id, event_type, entity_type, entity_id
            )
            VALUES (?, 'match.result_corrected', 'match', ?)
            """,
            (contest_id, match.id),
        ).lastrowid
        assert event_id is not None
        assert revise_existing_publication(
            connection,
            contest_id=contest_id,
            publication_type="match_result",
            entity_id=match.id,
            event_id=int(event_id),
            desired_action="publish",
        )

    assert finish_publication_success(
        database_path=database_path,
        publication=claimed,
        status="published",
    )
    with database_connection(database_path) as connection:
        publication = connection.execute(
            """
            SELECT desired_revision, settled_revision, delivery_status
            FROM contest_publications
            WHERE id = ?
            """,
            (claimed.id,),
        ).fetchone()
    assert publication["desired_revision"] == 2
    assert publication["settled_revision"] == 1
    assert publication["delivery_status"] == "pending"


def test_partial_delivery_retry_continues_with_unsent_part(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"
    contest_id, match = _create_contest_and_match(database_path)
    _enable_publications(database_path, contest_id=contest_id)
    _save_result(database_path, contest_id=contest_id, match=match)
    claimed = claim_next_publication(database_path=database_path)
    assert claimed is not None
    failing_bot = FailingBot(fail_on_send=2)

    with pytest.raises(TemporaryDeliveryError):
        asyncio.run(
            deliver_publication(
                bot=failing_bot,
                database_path=database_path,
                publication=claimed,
                desired_messages=("Первая часть", "Вторая часть"),
            )
        )
    assert len(failing_bot.sent) == 1
    assert finish_publication_failure(
        database_path=database_path,
        publication=claimed,
        error="temporary",
        permanent=False,
    )

    retry_claim = claim_next_publication(
        database_path=database_path,
        now_utc=datetime(2099, 1, 1, tzinfo=timezone.utc),
    )
    assert retry_claim is not None
    retry_bot = RecordingBot()
    asyncio.run(
        deliver_publication(
            bot=retry_bot,
            database_path=database_path,
            publication=retry_claim,
            desired_messages=("Первая часть", "Вторая часть"),
        )
    )
    assert len(retry_bot.sent) == 1
    assert retry_bot.sent[0]["text"] == "Вторая часть"
    assert finish_publication_success(
        database_path=database_path,
        publication=retry_claim,
        status="published",
    )


def _create_contest_and_match(database_path: Path):
    initialize_database(database_path)
    contest = create_world_cup_2026_contest(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        chat_title="Футбол",
        telegram_user_id=USER_ID,
        first_name="Анна",
        last_name="Иванова",
        username="anna",
        contest_name="ЧМ-2026",
        idempotency_key="contest-1",
        audit_actor=AUDIT_ACTOR,
    ).contest
    match = create_match(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest.id,
        telegram_user_id=USER_ID,
        first_name="Анна",
        last_name="Иванова",
        username="anna",
        home_team_name="Франция",
        away_team_name="Испания",
        starts_at_utc="2026-06-11T12:00:00Z",
        idempotency_key="match-1",
        audit_actor=AUDIT_ACTOR,
    ).match
    return contest.id, match


def _create_replacement_match(database_path: Path, *, contest_id: int):
    return create_match(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=USER_ID,
        first_name="Анна",
        last_name="Иванова",
        username="anna",
        home_team_name="Бразилия",
        away_team_name="Аргентина",
        starts_at_utc="2026-06-11T12:00:00Z",
        idempotency_key="replacement-match",
        audit_actor=AUDIT_ACTOR,
    ).match


def test_master_switch_settles_pending_result_without_backfill(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"
    contest_id, match = _create_contest_and_match(database_path)
    _enable_publications(database_path, contest_id=contest_id)
    _save_result(database_path, contest_id=contest_id, match=match)

    _disable_publications(database_path, contest_id=contest_id)
    bot = RecordingBot()
    assert (
        asyncio.run(
            process_due_contest_publications(
                bot=bot,
                database_path=database_path,
                max_publications=1,
            )
        )
        == 0
    )
    _enable_publications(database_path, contest_id=contest_id)
    assert (
        asyncio.run(
            process_due_contest_publications(
                bot=bot,
                database_path=database_path,
                max_publications=1,
            )
        )
        == 0
    )
    assert bot.sent == []

    with database_connection(database_path) as connection:
        publication = connection.execute(
            """
            SELECT desired_revision, settled_revision, delivery_status, claim_token
            FROM contest_publications
            WHERE contest_id = ? AND publication_type = 'match_result'
            """,
            (contest_id,),
        ).fetchone()
    assert tuple(publication) == (2, 2, "withdrawn", None)


def test_master_switch_during_send_compensates_untracked_message(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    contest_id, match = _create_contest_and_match(database_path)
    _enable_publications(database_path, contest_id=contest_id)
    _save_result(database_path, contest_id=contest_id, match=match)

    class DisablingDuringSendBot(RecordingBot):
        changed = False

        async def send_rich_message(
            self, chat_id: int, *, rich_message: InputRichMessage
        ) -> SentMessage:
            sent = await super().send_rich_message(chat_id, rich_message=rich_message)
            if not self.changed:
                self.changed = True
                _disable_publications(database_path, contest_id=contest_id)
            return sent

    bot = DisablingDuringSendBot()
    assert (
        asyncio.run(
            process_due_contest_publications(
                bot=bot,
                database_path=database_path,
                max_publications=1,
            )
        )
        == 0
    )
    assert bot.deleted == [{"chat_id": CHAT_ID, "message_id": 1000}]
    _enable_publications(database_path, contest_id=contest_id)
    assert (
        asyncio.run(
            process_due_contest_publications(bot=bot, database_path=database_path)
        )
        == 0
    )

    with database_connection(database_path) as connection:
        message_count = connection.execute(
            "SELECT COUNT(*) FROM contest_publication_messages"
        ).fetchone()[0]
        publication = connection.execute(
            """
            SELECT desired_revision, settled_revision, delivery_status, claim_token
            FROM contest_publications
            WHERE contest_id = ? AND publication_type = 'match_result'
            """,
            (contest_id,),
        ).fetchone()
    assert message_count == 0
    assert tuple(publication) == (2, 2, "withdrawn", None)


def test_master_switch_ignores_match_deletion_until_reenabled(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"
    contest_id, match = _create_contest_and_match(database_path)
    _enable_publications(database_path, contest_id=contest_id)
    _save_result(database_path, contest_id=contest_id, match=match)
    bot = RecordingBot()
    assert (
        asyncio.run(
            process_due_contest_publications(bot=bot, database_path=database_path)
        )
        == 1
    )

    _disable_publications(database_path, contest_id=contest_id)
    with database_connection(database_path) as connection:
        disabled = connection.execute(
            """
            SELECT id, desired_revision, settled_revision, delivery_status
            FROM contest_publications
            WHERE contest_id = ? AND publication_type = 'match_result'
            """,
            (contest_id,),
        ).fetchone()
    _delete_match(database_path, contest_id=contest_id, match_id=match.id)
    with database_connection(database_path) as connection:
        tombstone = connection.execute(
            """
            SELECT id, entity_id, desired_revision, settled_revision,
                   delivery_status, claim_token
            FROM contest_publications
            WHERE contest_id = ? AND publication_type = 'match_result'
            """,
            (contest_id,),
        ).fetchone()
        message_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM contest_publication_messages
            WHERE publication_id = ?
            """,
            (int(disabled["id"]),),
        ).fetchone()[0]
    assert tombstone["id"] == disabled["id"]
    assert tombstone["entity_id"] == -int(disabled["id"])
    assert tombstone["desired_revision"] == disabled["desired_revision"]
    assert tombstone["settled_revision"] == disabled["settled_revision"]
    assert tombstone["delivery_status"] == disabled["delivery_status"] == "published"
    assert tombstone["claim_token"] is None
    assert message_count == 1
    assert bot.deleted == []

    _enable_publications(database_path, contest_id=contest_id)
    assert (
        asyncio.run(
            process_due_contest_publications(bot=bot, database_path=database_path)
        )
        == 0
    )
    assert claim_next_publication(database_path=database_path) is None


def test_disabled_deletion_tombstone_allows_reused_match_id_publication(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    contest_id, first_match = _create_contest_and_match(database_path)
    _enable_publications(database_path, contest_id=contest_id)
    _save_result(database_path, contest_id=contest_id, match=first_match)
    bot = RecordingBot()
    assert (
        asyncio.run(
            process_due_contest_publications(bot=bot, database_path=database_path)
        )
        == 1
    )
    with database_connection(database_path) as connection:
        original = connection.execute(
            """
            SELECT publication.id, publication.desired_revision,
                   message.telegram_message_id
            FROM contest_publications AS publication
            JOIN contest_publication_messages AS message
              ON message.publication_id = publication.id
            WHERE publication.contest_id = ?
              AND publication.publication_type = 'match_result'
            """,
            (contest_id,),
        ).fetchone()

    _disable_publications(database_path, contest_id=contest_id)
    with database_connection(database_path) as connection:
        disabled_revision = connection.execute(
            """
            SELECT desired_revision
            FROM contest_publications
            WHERE id = ?
            """,
            (int(original["id"]),),
        ).fetchone()[0]
    _delete_match(database_path, contest_id=contest_id, match_id=first_match.id)
    with database_connection(database_path) as connection:
        tombstone = connection.execute(
            """
            SELECT id, entity_id, desired_revision, delivery_status, claim_token
            FROM contest_publications
            WHERE id = ?
            """,
            (int(original["id"]),),
        ).fetchone()
    assert bot.deleted == []
    assert tombstone["id"] == original["id"]
    assert tombstone["entity_id"] == -int(original["id"])
    assert tombstone["desired_revision"] == disabled_revision
    assert tombstone["delivery_status"] == "published"
    assert tombstone["claim_token"] is None

    _enable_publications(database_path, contest_id=contest_id)
    assert claim_next_publication(database_path=database_path) is None
    second_match = _create_replacement_match(database_path, contest_id=contest_id)
    assert second_match.id == first_match.id
    _save_result(database_path, contest_id=contest_id, match=second_match)

    with database_connection(database_path) as connection:
        publications = connection.execute(
            """
            SELECT id, entity_id, desired_revision, delivery_status
            FROM contest_publications
            WHERE contest_id = ? AND publication_type = 'match_result'
            ORDER BY id
            """,
            (contest_id,),
        ).fetchall()
        old_message_id = connection.execute(
            """
            SELECT telegram_message_id
            FROM contest_publication_messages
            WHERE publication_id = ?
            """,
            (int(original["id"]),),
        ).fetchone()[0]
    assert len(publications) == 2
    assert tuple(publications[0]) == (
        int(original["id"]),
        -int(original["id"]),
        disabled_revision,
        "published",
    )
    assert publications[1]["id"] != original["id"]
    assert publications[1]["entity_id"] == second_match.id
    assert publications[1]["desired_revision"] == 1
    assert publications[1]["delivery_status"] == "pending"
    assert old_message_id == original["telegram_message_id"] == 1000

    assert (
        asyncio.run(
            process_due_contest_publications(bot=bot, database_path=database_path)
        )
        == 1
    )
    assert len(bot.sent) == 2
    assert "Бразилия" in str(bot.sent[-1]["text"])
    assert bot.deleted == []


def test_disabled_deletion_removes_pending_record_before_match_id_reuse(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    contest_id, first_match = _create_contest_and_match(database_path)
    _enable_publications(database_path, contest_id=contest_id)
    _save_result(database_path, contest_id=contest_id, match=first_match)
    with database_connection(database_path) as connection:
        original_id = connection.execute(
            """
            SELECT id
            FROM contest_publications
            WHERE contest_id = ? AND publication_type = 'match_result'
            """,
            (contest_id,),
        ).fetchone()[0]

    _disable_publications(database_path, contest_id=contest_id)
    _delete_match(database_path, contest_id=contest_id, match_id=first_match.id)
    with database_connection(database_path) as connection:
        publication_count = connection.execute(
            "SELECT COUNT(*) FROM contest_publications WHERE id = ?",
            (original_id,),
        ).fetchone()[0]
    assert publication_count == 0

    _enable_publications(database_path, contest_id=contest_id)
    assert claim_next_publication(database_path=database_path) is None
    second_match = _create_replacement_match(database_path, contest_id=contest_id)
    assert second_match.id == first_match.id
    _save_result(database_path, contest_id=contest_id, match=second_match)
    with database_connection(database_path) as connection:
        publication = connection.execute(
            """
            SELECT id, entity_id, desired_revision, delivery_status
            FROM contest_publications
            WHERE contest_id = ? AND publication_type = 'match_result'
            """,
            (contest_id,),
        ).fetchone()
    assert publication["id"] > 0
    assert publication["entity_id"] == second_match.id
    assert publication["desired_revision"] == 1
    assert publication["delivery_status"] == "pending"


def test_master_switch_during_edit_restores_published_text(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"
    contest_id, match = _create_contest_and_match(database_path)
    _enable_publications(database_path, contest_id=contest_id)
    _save_result(database_path, contest_id=contest_id, match=match)
    bot = RecordingBot()
    assert (
        asyncio.run(
            process_due_contest_publications(bot=bot, database_path=database_path)
        )
        == 1
    )
    published_text = str(bot.sent[0]["text"])

    _save_result(
        database_path,
        contest_id=contest_id,
        match=match,
        home_score=1,
        away_score=0,
    )

    class DisablingDuringEditBot(RecordingBot):
        changed = False

        async def edit_message_text(
            self,
            *,
            chat_id: int,
            message_id: int,
            rich_message: InputRichMessage,
        ) -> bool:
            result = await super().edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                rich_message=rich_message,
            )
            if not self.changed:
                self.changed = True
                _disable_publications(database_path, contest_id=contest_id)
            return result

    changing_bot = DisablingDuringEditBot()
    assert (
        asyncio.run(
            process_due_contest_publications(
                bot=changing_bot, database_path=database_path
            )
        )
        == 0
    )
    assert len(changing_bot.edited) == 2
    assert changing_bot.edited[-1]["text"] == published_text
    assert changing_bot.deleted == []

    _enable_publications(database_path, contest_id=contest_id)
    assert (
        asyncio.run(
            process_due_contest_publications(
                bot=changing_bot, database_path=database_path
            )
        )
        == 0
    )


def test_stale_edit_withdraw_deletes_message_before_claim_release(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    contest_id, match = _create_contest_and_match(database_path)
    _enable_publications(database_path, contest_id=contest_id)
    _save_result(database_path, contest_id=contest_id, match=match)

    class DeletingDuringEditBot(RecordingBot):
        armed = False
        changed = False

        async def edit_message_text(
            self,
            *,
            chat_id: int,
            message_id: int,
            rich_message: InputRichMessage,
        ) -> bool:
            result = await super().edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                rich_message=rich_message,
            )
            if self.armed and not self.changed:
                self.changed = True
                _delete_match(database_path, contest_id=contest_id, match_id=match.id)
            return result

    bot = DeletingDuringEditBot()
    assert (
        asyncio.run(
            process_due_contest_publications(bot=bot, database_path=database_path)
        )
        == 1
    )
    _save_result(
        database_path,
        contest_id=contest_id,
        match=match,
        home_score=1,
        away_score=0,
    )
    bot.armed = True

    assert (
        asyncio.run(
            process_due_contest_publications(
                bot=bot,
                database_path=database_path,
                max_publications=1,
            )
        )
        == 0
    )
    assert bot.deleted == [{"chat_id": CHAT_ID, "message_id": 1000}]
    with database_connection(database_path) as connection:
        publication = connection.execute(
            """
            SELECT desired_revision, settled_revision, desired_action,
                   delivery_status, claim_token
            FROM contest_publications
            WHERE contest_id = ? AND publication_type = 'match_result'
            """,
            (contest_id,),
        ).fetchone()
    assert tuple(publication) == (3, 2, "withdraw", "pending", None)

    assert (
        asyncio.run(
            process_due_contest_publications(bot=bot, database_path=database_path)
        )
        == 1
    )


def test_stale_edit_withdraw_uses_fallback_when_delete_fails(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    contest_id, match = _create_contest_and_match(database_path)
    _enable_publications(database_path, contest_id=contest_id)
    _save_result(database_path, contest_id=contest_id, match=match)

    class NeutralizingDeleteDuringEditBot(RecordingBot):
        armed = False
        changed = False

        async def edit_message_text(
            self,
            *,
            chat_id: int,
            message_id: int,
            rich_message: InputRichMessage,
        ) -> bool:
            result = await super().edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                rich_message=rich_message,
            )
            if self.armed and not self.changed:
                self.changed = True
                _delete_match(database_path, contest_id=contest_id, match_id=match.id)
            return result

        async def delete_message(self, chat_id: int, message_id: int) -> bool:
            raise RuntimeError("delete unavailable")

    bot = NeutralizingDeleteDuringEditBot()
    assert (
        asyncio.run(
            process_due_contest_publications(bot=bot, database_path=database_path)
        )
        == 1
    )
    _save_result(
        database_path,
        contest_id=contest_id,
        match=match,
        home_score=1,
        away_score=0,
    )
    bot.armed = True

    assert (
        asyncio.run(
            process_due_contest_publications(
                bot=bot,
                database_path=database_path,
                max_publications=1,
            )
        )
        == 0
    )
    assert len(bot.edited) == 2
    assert "больше не актуальна" in str(bot.edited[-1]["text"])
    with database_connection(database_path) as connection:
        part = connection.execute(
            """
            SELECT message.part_status
            FROM contest_publication_messages AS message
            JOIN contest_publications AS publication
              ON publication.id = message.publication_id
            WHERE publication.contest_id = ?
              AND publication.publication_type = 'match_result'
            """,
            (contest_id,),
        ).fetchone()
        publication = connection.execute(
            """
            SELECT desired_revision, settled_revision, delivery_status, claim_token
            FROM contest_publications
            WHERE contest_id = ? AND publication_type = 'match_result'
            """,
            (contest_id,),
        ).fetchone()
    assert part["part_status"] == "retired"
    assert tuple(publication) == (3, 2, "pending", None)


def test_stale_publish_edit_is_reconciled_by_new_revision(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"
    contest_id, match = _create_contest_and_match(database_path)
    _enable_publications(database_path, contest_id=contest_id)
    _save_result(database_path, contest_id=contest_id, match=match)

    class CorrectingDuringEditBot(RecordingBot):
        armed = False
        changed = False

        async def edit_message_text(
            self,
            *,
            chat_id: int,
            message_id: int,
            rich_message: InputRichMessage,
        ) -> bool:
            result = await super().edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                rich_message=rich_message,
            )
            if self.armed and not self.changed:
                self.changed = True
                _save_result(
                    database_path,
                    contest_id=contest_id,
                    match=match,
                    home_score=3,
                    away_score=0,
                )
            return result

    bot = CorrectingDuringEditBot()
    assert (
        asyncio.run(
            process_due_contest_publications(bot=bot, database_path=database_path)
        )
        == 1
    )
    _save_result(
        database_path,
        contest_id=contest_id,
        match=match,
        home_score=1,
        away_score=0,
    )
    bot.armed = True

    assert (
        asyncio.run(
            process_due_contest_publications(
                bot=bot,
                database_path=database_path,
                max_publications=1,
            )
        )
        == 0
    )
    with database_connection(database_path) as connection:
        publication = connection.execute(
            """
            SELECT desired_revision, settled_revision, desired_action,
                   delivery_status, claim_token
            FROM contest_publications
            WHERE contest_id = ? AND publication_type = 'match_result'
            """,
            (contest_id,),
        ).fetchone()
    assert tuple(publication) == (3, 2, "publish", "pending", None)

    assert (
        asyncio.run(
            process_due_contest_publications(bot=bot, database_path=database_path)
        )
        == 1
    )
    assert len(bot.edited) == 2
    assert "3:0" in str(bot.edited[-1]["text"])


def test_champion_result_is_published_immediately_corrected_and_not_republished(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    contest_id, match = _create_contest_and_match(database_path)
    _enable_publications(database_path, contest_id=contest_id)
    save_champion_prediction_settings(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=USER_ID,
        first_name="Анна",
        last_name="Иванова",
        username="anna",
        enabled=True,
        deadline_at="2026-06-11T11:00:00Z",
        points=5,
        now_utc=_datetime(10),
        audit_actor=AUDIT_ACTOR,
    )
    save_champion_prediction(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=USER_ID,
        first_name="Анна",
        last_name="Иванова",
        username="anna",
        predicted_team_id=match.home_team_id,
        now_utc=_datetime(10),
    )
    _save_result(database_path, contest_id=contest_id, match=match)

    bot = RecordingBot()
    asyncio.run(process_due_contest_publications(bot=bot, database_path=database_path))
    sent_before_champion = len(bot.sent)

    save_contest_champion(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=USER_ID,
        first_name="Анна",
        last_name="Иванова",
        username="anna",
        champion_team_id=match.home_team_id,
        now_utc=_datetime(12),
        audit_actor=AUDIT_ACTOR,
    )
    with database_connection(database_path) as connection:
        initial_publication = tuple(
            connection.execute(
                """
                SELECT desired_revision, desired_action, reconcile_at
                FROM contest_publications
                WHERE publication_type = 'champion_result'
                """
            ).fetchone()
        )
        event_count = connection.execute(
            """
            SELECT COUNT(*) FROM event_log
            WHERE contest_id = ?
              AND event_type IN (
                  'contest.champion_recorded',
                  'contest.champion_corrected'
              )
            """,
            (contest_id,),
        ).fetchone()[0]
    assert initial_publication == (1, "publish", None)
    assert (
        asyncio.run(
            process_due_contest_publications(bot=bot, database_path=database_path)
        )
        == 1
    )
    assert len(bot.sent) == sent_before_champion + 1
    assert "Чемпион турнира" in str(bot.sent[-1]["text"])

    save_contest_champion(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=USER_ID,
        first_name="Анна",
        last_name="Иванова",
        username="anna",
        champion_team_id=match.home_team_id,
        now_utc=_datetime(12),
        audit_actor=AUDIT_ACTOR,
    )
    with database_connection(database_path) as connection:
        same_publication = tuple(
            connection.execute(
                """
                SELECT desired_revision, desired_action, reconcile_at
                FROM contest_publications
                WHERE publication_type = 'champion_result'
                """
            ).fetchone()
        )
        same_event_count = connection.execute(
            """
            SELECT COUNT(*) FROM event_log
            WHERE contest_id = ?
              AND event_type IN (
                  'contest.champion_recorded',
                  'contest.champion_corrected'
              )
            """,
            (contest_id,),
        ).fetchone()[0]
    assert same_publication == initial_publication
    assert same_event_count == event_count
    assert (
        asyncio.run(
            process_due_contest_publications(bot=bot, database_path=database_path)
        )
        == 0
    )

    save_contest_champion(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=USER_ID,
        first_name="Анна",
        last_name="Иванова",
        username="anna",
        champion_team_id=match.away_team_id,
        now_utc=_datetime(12),
        audit_actor=AUDIT_ACTOR,
    )
    assert (
        asyncio.run(
            process_due_contest_publications(bot=bot, database_path=database_path)
        )
        == 1
    )
    assert len(bot.sent) == sent_before_champion + 1
    assert len(bot.edited) == 1
    with database_connection(database_path) as connection:
        away_team_name = connection.execute(
            "SELECT name FROM teams WHERE id = ?",
            (match.away_team_id,),
        ).fetchone()[0]
    assert str(away_team_name) in str(bot.edited[0]["text"])

    with database_connection(database_path) as connection:
        revisions_before_completion = {
            str(row["publication_type"]): int(row["desired_revision"])
            for row in connection.execute(
                """
                SELECT publication_type, desired_revision
                FROM contest_publications
                WHERE publication_type IN (
                    'champion_predictions',
                    'champion_result'
                )
                """
            )
        }
    complete_contest(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=USER_ID,
        first_name="Анна",
        last_name="Иванова",
        username="anna",
        audit_actor=AUDIT_ACTOR,
    )
    with database_connection(database_path) as connection:
        revisions_after_completion = {
            str(row["publication_type"]): int(row["desired_revision"])
            for row in connection.execute(
                """
                SELECT publication_type, desired_revision
                FROM contest_publications
                WHERE publication_type IN (
                    'champion_predictions',
                    'champion_result'
                )
                """
            )
        }
    assert revisions_after_completion == revisions_before_completion

    sent_before_completion = len(bot.sent)
    assert (
        asyncio.run(
            process_due_contest_publications(bot=bot, database_path=database_path)
        )
        == 1
    )
    assert len(bot.sent) == sent_before_completion + 1
    assert "Итоговый рейтинг" in str(bot.sent[-1]["text"])


@pytest.mark.parametrize(
    ("enabled", "deadline_at", "points"),
    (
        (True, "2026-06-12T11:00:00Z", 5),
        (False, None, 5),
        (True, "2026-06-11T11:00:00Z", 9),
    ),
    ids=("deadline", "disable", "points"),
)
def test_champion_settings_are_locked_without_side_effects_after_result(
    tmp_path: Path,
    enabled: bool,
    deadline_at: str | None,
    points: int,
) -> None:
    database_path = tmp_path / "predictor.db"
    contest_id, _ = _prepare_champion_result(database_path)

    with database_connection(database_path) as connection:
        settings_before = tuple(
            connection.execute(
                """
                SELECT
                    champion_prediction_enabled,
                    champion_prediction_deadline_at,
                    champion_prediction_points,
                    champion_team_id
                FROM contests
                WHERE id = ?
                """,
                (contest_id,),
            ).fetchone()
        )
        event_count_before = connection.execute(
            "SELECT COUNT(*) FROM event_log WHERE contest_id = ?",
            (contest_id,),
        ).fetchone()[0]
        revisions_before = {
            str(row["publication_type"]): (
                int(row["desired_revision"]),
                int(row["latest_event_id"]),
            )
            for row in connection.execute(
                """
                SELECT publication_type, desired_revision, latest_event_id
                FROM contest_publications
                WHERE contest_id = ?
                  AND publication_type IN (
                      'champion_predictions',
                      'champion_result'
                  )
                """,
                (contest_id,),
            )
        }
    assert set(revisions_before) == {"champion_predictions", "champion_result"}

    with pytest.raises(
        ChampionPredictionSettingsLockedError,
        match=(
            "Настройки прогноза на чемпиона нельзя изменить после указания "
            "фактического чемпиона"
        ),
    ):
        save_champion_prediction_settings(
            database_path=database_path,
            telegram_chat_id=CHAT_ID,
            contest_id=contest_id,
            telegram_user_id=USER_ID,
            first_name="Анна",
            last_name="Иванова",
            username="anna",
            enabled=enabled,
            deadline_at=deadline_at,
            points=points,
            now_utc=_datetime(12),
            audit_actor=AUDIT_ACTOR,
        )

    with database_connection(database_path) as connection:
        settings_after = tuple(
            connection.execute(
                """
                SELECT
                    champion_prediction_enabled,
                    champion_prediction_deadline_at,
                    champion_prediction_points,
                    champion_team_id
                FROM contests
                WHERE id = ?
                """,
                (contest_id,),
            ).fetchone()
        )
        event_count_after = connection.execute(
            "SELECT COUNT(*) FROM event_log WHERE contest_id = ?",
            (contest_id,),
        ).fetchone()[0]
        revisions_after = {
            str(row["publication_type"]): (
                int(row["desired_revision"]),
                int(row["latest_event_id"]),
            )
            for row in connection.execute(
                """
                SELECT publication_type, desired_revision, latest_event_id
                FROM contest_publications
                WHERE contest_id = ?
                  AND publication_type IN (
                      'champion_predictions',
                      'champion_result'
                  )
                """,
                (contest_id,),
            )
        }

    assert settings_after == settings_before
    assert settings_after[3] is not None
    assert event_count_after == event_count_before
    assert revisions_after == revisions_before


def test_champion_renderer_rejects_open_deadline_for_completed_contest(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    contest_id, match = _create_contest_and_match(database_path)
    _enable_publications(database_path, contest_id=contest_id)
    save_champion_prediction_settings(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=USER_ID,
        first_name="Анна",
        last_name="Иванова",
        username="anna",
        enabled=True,
        deadline_at="2099-01-01T00:00:00Z",
        points=5,
        audit_actor=AUDIT_ACTOR,
    )
    save_champion_prediction(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=USER_ID,
        first_name="Анна",
        last_name="Иванова",
        username="anna",
        predicted_team_id=match.home_team_id,
        now_utc=_datetime(10),
    )
    _save_result(database_path, contest_id=contest_id, match=match)
    with database_connection(database_path) as connection:
        connection.execute(
            "UPDATE contests SET champion_team_id = ?, is_active = 0 WHERE id = ?",
            (match.home_team_id, contest_id),
        )
        event_id = connection.execute(
            """
            INSERT INTO event_log (contest_id, event_type, entity_type, entity_id)
            VALUES (?, 'legacy.champion_recorded', 'contest', ?)
            """,
            (contest_id, contest_id),
        ).lastrowid
        assert event_id is not None
        create_or_revise_champion_publication(
            connection,
            contest_id=contest_id,
            event_id=int(event_id),
            was_created=True,
            now_utc=_datetime(12),
        )
    assert (
        asyncio.run(
            process_due_contest_publications(
                bot=RecordingBot(),
                database_path=database_path,
                max_publications=2,
            )
        )
        == 2
    )
    with database_connection(database_path) as connection:
        champion_result = connection.execute(
            """
            SELECT id, contest_id, publication_type, entity_id, desired_revision
            FROM contest_publications
            WHERE publication_type = 'champion_result'
            """
        ).fetchone()
    assert champion_result is not None
    forced_publish = ClaimedPublication(
        id=int(champion_result["id"]),
        contest_id=int(champion_result["contest_id"]),
        publication_type=str(champion_result["publication_type"]),  # type: ignore[arg-type]
        entity_id=int(champion_result["entity_id"]),
        desired_revision=int(champion_result["desired_revision"]),
        desired_action="publish",
        claim_token="forced-render",
    )

    with pytest.raises(StalePublicationRevision):
        render_publication_messages(
            database_path=database_path,
            publication=forced_publish,
            now_utc=_datetime(12),
        )


def test_master_switch_cancels_existing_champion_reconciliation(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"
    contest_id, _ = _create_contest_and_match(database_path)
    _enable_publications(database_path, contest_id=contest_id)
    future = serialize_service_time(datetime(2099, 1, 1, tzinfo=timezone.utc))
    now = serialize_service_time(_datetime(12))
    with database_connection(database_path) as connection:
        event_id = connection.execute(
            """
            INSERT INTO event_log (contest_id, event_type, entity_type, entity_id)
            VALUES (?, 'legacy.champion_reconcile', 'contest', ?)
            """,
            (contest_id, contest_id),
        ).lastrowid
        connection.execute(
            """
            INSERT INTO contest_publications (
                contest_id, publication_type, entity_id, desired_revision,
                settled_revision, desired_action, delivery_status, first_event_id,
                latest_event_id, reconcile_at, created_at, updated_at
            ) VALUES (?, 'champion_result', ?, 1, 1, 'withdraw', 'withdrawn',
                      ?, ?, ?, ?, ?)
            """,
            (contest_id, contest_id, event_id, event_id, future, now, now),
        )

    _disable_publications(database_path, contest_id=contest_id)
    _enable_publications(database_path, contest_id=contest_id)
    assert (
        claim_next_publication(
            database_path=database_path,
            now_utc=datetime(2099, 1, 2, tzinfo=timezone.utc),
        )
        is None
    )
    with database_connection(database_path) as connection:
        publication = connection.execute(
            """
            SELECT desired_revision, settled_revision, reconcile_at
            FROM contest_publications
            WHERE contest_id = ? AND publication_type = 'champion_result'
            """,
            (contest_id,),
        ).fetchone()
    assert tuple(publication) == (2, 2, None)


def test_master_switch_enabled_before_deadline_restores_future_publication(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    contest_id, match = _create_contest_and_match(database_path)
    save_champion_prediction_settings(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=USER_ID,
        first_name="Анна",
        last_name="Иванова",
        username="anna",
        enabled=True,
        deadline_at="2026-06-11T11:00:00Z",
        points=5,
        audit_actor=AUDIT_ACTOR,
    )
    save_champion_prediction(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=USER_ID,
        first_name="Анна",
        last_name="Иванова",
        username="anna",
        predicted_team_id=match.home_team_id,
        now_utc=_datetime(10),
    )
    _enable_publications(database_path, contest_id=contest_id)
    bot = RecordingBot()
    assert (
        asyncio.run(
            process_due_contest_publications(bot=bot, database_path=database_path)
        )
        == 1
    )
    assert "Прогнозы на чемпиона" in str(bot.sent[0]["text"])

    _save_result(database_path, contest_id=contest_id, match=match)
    save_contest_champion(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=USER_ID,
        first_name="Анна",
        last_name="Иванова",
        username="anna",
        champion_team_id=match.home_team_id,
        audit_actor=AUDIT_ACTOR,
    )
    bot = RecordingBot()
    assert (
        asyncio.run(
            process_due_contest_publications(bot=bot, database_path=database_path)
        )
        == 2
    )
    assert len(bot.sent) == 2
    assert "Чемпион турнира" in str(bot.sent[1]["text"])


def test_completion_while_master_disabled_sends_nothing(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"
    contest_id, match = _prepare_champion_result(database_path)
    _disable_publications(database_path, contest_id=contest_id)
    complete_contest(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=USER_ID,
        first_name="Анна",
        last_name="Иванова",
        username="anna",
        audit_actor=AUDIT_ACTOR,
    )
    bot = RecordingBot()
    assert (
        asyncio.run(
            process_due_contest_publications(bot=bot, database_path=database_path)
        )
        == 0
    )
    assert bot.sent == []


def test_completion_without_champion_prediction_creates_no_prediction_publication(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    contest_id, match = _create_contest_and_match(database_path)
    _enable_publications(database_path, contest_id=contest_id)
    _save_result(database_path, contest_id=contest_id, match=match)

    complete_contest(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=USER_ID,
        first_name="Анна",
        last_name="Иванова",
        username="anna",
        audit_actor=AUDIT_ACTOR,
    )

    with database_connection(database_path) as connection:
        publication_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM contest_publications
            WHERE contest_id = ?
              AND publication_type = 'champion_predictions'
            """,
            (contest_id,),
        ).fetchone()[0]

    assert publication_count == 0


def test_master_reenable_does_not_backfill_champion_result(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    contest_id, match = _create_contest_and_match(database_path)
    save_champion_prediction_settings(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=USER_ID,
        first_name="Анна",
        last_name="Иванова",
        username="anna",
        enabled=True,
        deadline_at="2026-06-11T11:00:00Z",
        points=5,
        audit_actor=AUDIT_ACTOR,
    )
    save_champion_prediction(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=USER_ID,
        first_name="Анна",
        last_name="Иванова",
        username="anna",
        predicted_team_id=match.home_team_id,
        now_utc=_datetime(10),
    )
    _save_result(database_path, contest_id=contest_id, match=match)
    save_contest_champion(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=USER_ID,
        first_name="Анна",
        last_name="Иванова",
        username="anna",
        champion_team_id=match.home_team_id,
        audit_actor=AUDIT_ACTOR,
    )
    with database_connection(database_path) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM contest_publications").fetchone()[
                0
            ]
            == 0
        )

    _enable_publications(database_path, contest_id=contest_id)
    save_contest_champion(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=USER_ID,
        first_name="Анна",
        last_name="Иванова",
        username="anna",
        champion_team_id=match.away_team_id,
        now_utc=_datetime(12),
        audit_actor=AUDIT_ACTOR,
    )
    with database_connection(database_path) as connection:
        assert (
            connection.execute(
                """
            SELECT COUNT(*) FROM contest_publications
            WHERE publication_type = 'champion_result'
            """
            ).fetchone()[0]
            == 0
        )

    complete_contest(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=USER_ID,
        first_name="Анна",
        last_name="Иванова",
        username="anna",
        audit_actor=AUDIT_ACTOR,
    )
    bot = RecordingBot()
    assert (
        asyncio.run(
            process_due_contest_publications(bot=bot, database_path=database_path)
        )
        == 2
    )
    assert "Прогнозы на чемпиона" in str(bot.sent[0]["text"])
    assert "Итоговый рейтинг" in str(bot.sent[1]["text"])


def test_completed_contest_final_dependencies_override_adverse_event_order(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    contest_id, match = _create_contest_and_match(database_path)
    save_champion_prediction_settings(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=USER_ID,
        first_name="Анна",
        last_name="Иванова",
        username="anna",
        enabled=True,
        deadline_at="2026-06-11T11:00:00Z",
        points=5,
        now_utc=_datetime(10),
        audit_actor=AUDIT_ACTOR,
    )
    save_champion_prediction(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=USER_ID,
        first_name="Анна",
        last_name="Иванова",
        username="anna",
        predicted_team_id=match.home_team_id,
        now_utc=_datetime(10),
    )
    _save_result(database_path, contest_id=contest_id, match=match)
    save_match_prediction_publication_settings(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=USER_ID,
        first_name="Анна",
        last_name="Иванова",
        username="anna",
        enabled=True,
        now_utc=_datetime(12),
        audit_actor=AUDIT_ACTOR,
    )
    save_contest_champion(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=USER_ID,
        first_name="Анна",
        last_name="Иванова",
        username="anna",
        champion_team_id=match.home_team_id,
        audit_actor=AUDIT_ACTOR,
    )
    with database_connection(database_path) as connection:
        event_id = connection.execute(
            """
            INSERT INTO event_log (
                contest_id, event_type, entity_type, entity_id
            )
            VALUES (?, 'test.adverse_champion_predictions_order', 'contest', ?)
            """,
            (contest_id, contest_id),
        ).lastrowid
        assert event_id is not None
        create_or_revise_champion_predictions_publication(
            connection,
            contest_id=contest_id,
            event_id=int(event_id),
            now_utc=_datetime(12),
        )
        first_events = {
            str(row["publication_type"]): int(row["first_event_id"])
            for row in connection.execute(
                """
                SELECT publication_type, first_event_id
                FROM contest_publications
                """
            )
        }
    assert first_events["champion_result"] < first_events["champion_predictions"]

    complete_contest(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=USER_ID,
        first_name="Анна",
        last_name="Иванова",
        username="anna",
        audit_actor=AUDIT_ACTOR,
    )
    bot = RecordingBot()
    assert (
        asyncio.run(
            process_due_contest_publications(bot=bot, database_path=database_path)
        )
        == 3
    )
    assert "Прогнозы на чемпиона" in str(bot.sent[0]["text"])
    assert "Чемпион турнира" in str(bot.sent[1]["text"])
    assert "Итоговый рейтинг" in str(bot.sent[2]["text"])


def test_legacy_champion_result_reconciliation_is_restored_and_published(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    contest_id, match = _create_contest_and_match(database_path)
    _enable_publications(database_path, contest_id=contest_id)
    save_champion_prediction_settings(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=USER_ID,
        first_name="Анна",
        last_name="Иванова",
        username="anna",
        enabled=True,
        deadline_at="2026-06-11T11:00:00Z",
        points=5,
        now_utc=_datetime(9),
        audit_actor=AUDIT_ACTOR,
    )
    save_champion_prediction(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=USER_ID,
        first_name="Анна",
        last_name="Иванова",
        username="anna",
        predicted_team_id=match.home_team_id,
        now_utc=_datetime(10),
    )
    _save_result(database_path, contest_id=contest_id, match=match)

    assert (
        asyncio.run(
            process_due_contest_publications(
                bot=RecordingBot(),
                database_path=database_path,
            )
        )
        == 2
    )

    now_value = serialize_service_time(_datetime(10))
    with database_connection(database_path) as connection:
        connection.execute(
            "UPDATE contests SET champion_team_id = ? WHERE id = ?",
            (match.home_team_id, contest_id),
        )
        event_id = connection.execute(
            """
            INSERT INTO event_log (
                contest_id, event_type, entity_type, entity_id
            )
            VALUES (?, 'legacy.champion_recorded', 'contest', ?)
            """,
            (contest_id, contest_id),
        ).lastrowid
        assert event_id is not None
        publication_id = connection.execute(
            """
            INSERT INTO contest_publications (
                contest_id, publication_type, entity_id, desired_revision,
                settled_revision, desired_action, delivery_status,
                first_event_id, latest_event_id, reconcile_at, created_at,
                updated_at
            )
            VALUES (?, 'champion_result', ?, 1, 1, 'withdraw', 'withdrawn',
                    ?, ?, NULL, ?, ?)
            """,
            (contest_id, contest_id, event_id, event_id, now_value, now_value),
        ).lastrowid
        assert publication_id is not None

    assert (
        restore_legacy_champion_result_reconciliations(
            database_path=database_path,
            now_utc=_datetime(10),
        )
        == 1
    )
    with database_connection(database_path) as connection:
        restored_publication = tuple(
            connection.execute(
                """
                SELECT
                    desired_revision,
                    settled_revision,
                    desired_action,
                    first_event_id,
                    latest_event_id,
                    reconcile_at,
                    updated_at
                FROM contest_publications
                WHERE id = ?
                """,
                (publication_id,),
            ).fetchone()
        )
    assert restored_publication == (
        1,
        1,
        "withdraw",
        event_id,
        event_id,
        serialize_service_time(_datetime(11)),
        serialize_service_time(_datetime(10)),
    )
    assert (
        claim_next_publication(
            database_path=database_path,
            now_utc=_datetime(10),
        )
        is None
    )

    assert (
        restore_legacy_champion_result_reconciliations(
            database_path=database_path,
            now_utc=_datetime(10),
        )
        == 0
    )
    with database_connection(database_path) as connection:
        repeated_publication = tuple(
            connection.execute(
                """
                SELECT
                    desired_revision,
                    settled_revision,
                    desired_action,
                    first_event_id,
                    latest_event_id,
                    reconcile_at,
                    updated_at
                FROM contest_publications
                WHERE id = ?
                """,
                (publication_id,),
            ).fetchone()
        )
    assert repeated_publication == restored_publication

    result_bot = RecordingBot()
    assert (
        asyncio.run(
            process_due_contest_publications(
                bot=result_bot,
                database_path=database_path,
            )
        )
        == 1
    )
    assert len(result_bot.sent) == 1
    assert "Чемпион турнира" in str(result_bot.sent[0]["text"])

    with database_connection(database_path) as connection:
        champion_publications = connection.execute(
            """
            SELECT id, desired_revision, settled_revision, desired_action,
                   reconcile_at
            FROM contest_publications
            WHERE contest_id = ? AND publication_type = 'champion_result'
            """,
            (contest_id,),
        ).fetchall()
        contest_is_active = connection.execute(
            "SELECT is_active FROM contests WHERE id = ?",
            (contest_id,),
        ).fetchone()[0]
        champion_event_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM event_log
            WHERE contest_id = ?
              AND event_type IN (
                  'contest.champion_recorded',
                  'contest.champion_corrected'
              )
            """,
            (contest_id,),
        ).fetchone()[0]
    assert [tuple(row) for row in champion_publications] == [
        (publication_id, 2, 2, "publish", None)
    ]
    assert contest_is_active == 1
    assert champion_event_count == 0

    complete_contest(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=USER_ID,
        first_name="Анна",
        last_name="Иванова",
        username="anna",
        now_utc=_datetime(12),
        audit_actor=AUDIT_ACTOR,
    )

    final_bot = RecordingBot()
    assert (
        asyncio.run(
            process_due_contest_publications(
                bot=final_bot,
                database_path=database_path,
            )
        )
        == 1
    )
    assert len(final_bot.sent) == 1
    assert "Итоговый рейтинг" in str(final_bot.sent[0]["text"])


def test_legacy_reconciliation_does_not_create_missing_champion_result(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    contest_id, match = _create_contest_and_match(database_path)
    save_champion_prediction_settings(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=USER_ID,
        first_name="Анна",
        last_name="Иванова",
        username="anna",
        enabled=True,
        deadline_at="2026-06-11T11:00:00Z",
        points=5,
        now_utc=_datetime(9),
        audit_actor=AUDIT_ACTOR,
    )
    _save_result(database_path, contest_id=contest_id, match=match)
    with database_connection(database_path) as connection:
        connection.execute(
            "UPDATE contests SET champion_team_id = ? WHERE id = ?",
            (match.home_team_id, contest_id),
        )

    _enable_publications(database_path, contest_id=contest_id)
    assert (
        restore_legacy_champion_result_reconciliations(
            database_path=database_path,
            now_utc=_datetime(10),
        )
        == 0
    )
    with database_connection(database_path) as connection:
        champion_result_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM contest_publications
            WHERE contest_id = ? AND publication_type = 'champion_result'
            """,
            (contest_id,),
        ).fetchone()[0]
    assert champion_result_count == 0


def test_completion_before_champion_deadline_rejects_legacy_champion(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    contest_id, match = _create_contest_and_match(database_path)
    save_champion_prediction_settings(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=USER_ID,
        first_name="Анна",
        last_name="Иванова",
        username="anna",
        enabled=True,
        deadline_at="2026-06-11T11:00:00Z",
        points=5,
        now_utc=_datetime(9),
        audit_actor=AUDIT_ACTOR,
    )
    _save_result(database_path, contest_id=contest_id, match=match)
    with database_connection(database_path) as connection:
        connection.execute(
            "UPDATE contests SET champion_team_id = ? WHERE id = ?",
            (match.home_team_id, contest_id),
        )

    with pytest.raises(
        ContestCompletionUnavailableError,
        match="Конкурс можно завершить после закрытия прогнозов на чемпиона",
    ):
        complete_contest(
            database_path=database_path,
            telegram_chat_id=CHAT_ID,
            contest_id=contest_id,
            telegram_user_id=USER_ID,
            first_name="Анна",
            last_name="Иванова",
            username="anna",
            now_utc=_datetime(10),
            audit_actor=AUDIT_ACTOR,
        )

    with database_connection(database_path) as connection:
        contest_is_active = connection.execute(
            "SELECT is_active FROM contests WHERE id = ?",
            (contest_id,),
        ).fetchone()[0]
        completion_event_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM event_log
            WHERE contest_id = ? AND event_type = 'contest.completed'
            """,
            (contest_id,),
        ).fetchone()[0]
    assert contest_is_active == 1
    assert completion_event_count == 0


def _enable_publications(database_path: Path, *, contest_id: int) -> None:
    save_match_prediction_publication_settings(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=USER_ID,
        first_name="Анна",
        last_name="Иванова",
        username="anna",
        enabled=True,
        now_utc=_datetime(9),
        audit_actor=AUDIT_ACTOR,
    )


def _disable_publications(database_path: Path, *, contest_id: int) -> None:
    save_match_prediction_publication_settings(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=USER_ID,
        first_name="Анна",
        last_name="Иванова",
        username="anna",
        enabled=False,
        now_utc=_datetime(13),
        audit_actor=AUDIT_ACTOR,
    )


def _prepare_champion_result(database_path: Path):
    contest_id, match = _create_contest_and_match(database_path)
    _enable_publications(database_path, contest_id=contest_id)
    save_champion_prediction_settings(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=USER_ID,
        first_name="Анна",
        last_name="Иванова",
        username="anna",
        enabled=True,
        deadline_at="2026-06-11T11:00:00Z",
        points=5,
        audit_actor=AUDIT_ACTOR,
    )
    save_champion_prediction(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=USER_ID,
        first_name="Анна",
        last_name="Иванова",
        username="anna",
        predicted_team_id=match.home_team_id,
        now_utc=_datetime(10),
    )
    _save_result(database_path, contest_id=contest_id, match=match)
    save_contest_champion(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=USER_ID,
        first_name="Анна",
        last_name="Иванова",
        username="anna",
        champion_team_id=match.home_team_id,
        audit_actor=AUDIT_ACTOR,
    )
    return contest_id, match


def _save_result(
    database_path: Path,
    *,
    contest_id: int,
    match,
    home_score: int = 2,
    away_score: int = 1,
) -> None:
    save_match_result(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        match_id=match.id,
        telegram_user_id=USER_ID,
        first_name="Анна",
        last_name="Иванова",
        username="anna",
        home_score=home_score,
        away_score=away_score,
        advancing_team_id=match.home_team_id,
        now_utc=_datetime(12),
        audit_actor=AUDIT_ACTOR,
    )


def _delete_match(database_path: Path, *, contest_id: int, match_id: int) -> None:
    delete_match(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        match_id=match_id,
        telegram_user_id=USER_ID,
        first_name="Анна",
        last_name="Иванова",
        username="anna",
        audit_actor=AUDIT_ACTOR,
    )


def _datetime(hour: int) -> datetime:
    return datetime(2026, 6, 11, hour, tzinfo=timezone.utc)
