from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.contest_service import (
    complete_contest,
    create_match,
    create_world_cup_2026_contest,
    delete_match,
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
    StalePublicationRevision,
    claim_next_publication,
    finish_publication_failure,
    finish_publication_success,
    revise_existing_publication,
    serialize_service_time,
)
from app.publication_worker import process_due_contest_publications


CHAT_ID = -1001234567890
USER_ID = 123456789


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
        self.sent.append({"chat_id": chat_id, "text": text, "parse_mode": parse_mode})
        return SentMessage(message_id=message_id)

    async def edit_message_text(
        self,
        text: str,
        *,
        chat_id: int,
        message_id: int,
        parse_mode: str,
    ) -> bool:
        self.edited.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "parse_mode": parse_mode,
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

    async def send_message(
        self, chat_id: int, text: str, *, parse_mode: str
    ) -> SentMessage:
        self.send_attempts += 1
        if self.send_attempts == self.fail_on_send:
            raise RuntimeError("Telegram is temporarily unavailable.")
        return await super().send_message(chat_id, text, parse_mode=parse_mode)


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
    assert "2:1" in str(bot.sent[0]["text"])
    assert "4 балла" in str(bot.sent[0]["text"])
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
        async def send_message(
            self, chat_id: int, text: str, *, parse_mode: str
        ) -> SentMessage:
            sent = await super().send_message(
                chat_id,
                text,
                parse_mode=parse_mode,
            )
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
    )
    complete_contest(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=USER_ID,
        first_name="Анна",
        last_name="Иванова",
        username="anna",
    )

    bot = RecordingBot()
    assert (
        asyncio.run(
            process_due_contest_publications(
                bot=bot,
                database_path=database_path,
            )
        )
        == 3
    )
    assert len(bot.sent) == 3
    assert "Результаты прогнозов" in str(bot.sent[0]["text"])
    assert "Чемпион турнира" in str(bot.sent[1]["text"])
    assert "Итоговый рейтинг" in str(bot.sent[2]["text"])


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

        async def send_message(
            self, chat_id: int, text: str, *, parse_mode: str
        ) -> SentMessage:
            sent = await super().send_message(chat_id, text, parse_mode=parse_mode)
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
            text: str,
            *,
            chat_id: int,
            message_id: int,
            parse_mode: str,
        ) -> bool:
            result = await super().edit_message_text(
                text,
                chat_id=chat_id,
                message_id=message_id,
                parse_mode=parse_mode,
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
            text: str,
            *,
            chat_id: int,
            message_id: int,
            parse_mode: str,
        ) -> bool:
            result = await super().edit_message_text(
                text,
                chat_id=chat_id,
                message_id=message_id,
                parse_mode=parse_mode,
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
            text: str,
            *,
            chat_id: int,
            message_id: int,
            parse_mode: str,
        ) -> bool:
            result = await super().edit_message_text(
                text,
                chat_id=chat_id,
                message_id=message_id,
                parse_mode=parse_mode,
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
            text: str,
            *,
            chat_id: int,
            message_id: int,
            parse_mode: str,
        ) -> bool:
            result = await super().edit_message_text(
                text,
                chat_id=chat_id,
                message_id=message_id,
                parse_mode=parse_mode,
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


def test_champion_renderer_rejects_open_deadline_for_active_contest(
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
    )
    assert (
        asyncio.run(
            process_due_contest_publications(
                bot=RecordingBot(),
                database_path=database_path,
                max_publications=1,
            )
        )
        == 1
    )
    claim = claim_next_publication(database_path=database_path)
    assert claim is not None and claim.publication_type == "champion_result"
    forced_publish = claim.__class__(
        id=claim.id,
        contest_id=claim.contest_id,
        publication_type=claim.publication_type,
        entity_id=claim.entity_id,
        desired_revision=claim.desired_revision,
        desired_action="publish",
        claim_token=claim.claim_token,
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


def test_champion_deadline_while_master_disabled_never_backfills(
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
    assert (
        asyncio.run(
            process_due_contest_publications(
                bot=RecordingBot(), database_path=database_path
            )
        )
        == 0
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
    )
    bot = RecordingBot()
    assert (
        asyncio.run(
            process_due_contest_publications(bot=bot, database_path=database_path)
        )
        == 2
    )
    assert len(bot.sent) == 1
    assert "Чемпион турнира" not in str(bot.sent[0]["text"])


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
    )
    bot = RecordingBot()
    assert (
        asyncio.run(
            process_due_contest_publications(bot=bot, database_path=database_path)
        )
        == 0
    )
    assert bot.sent == []


def test_completion_creates_terminal_champion_after_master_reenabled(
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
    )
    with database_connection(database_path) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM contest_publications").fetchone()[
                0
            ]
            == 0
        )

    _enable_publications(database_path, contest_id=contest_id)
    complete_contest(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=USER_ID,
        first_name="Анна",
        last_name="Иванова",
        username="anna",
    )
    bot = RecordingBot()
    assert (
        asyncio.run(
            process_due_contest_publications(bot=bot, database_path=database_path)
        )
        == 2
    )
    assert "Чемпион турнира" in str(bot.sent[0]["text"])
    assert "Итоговый рейтинг" in str(bot.sent[1]["text"])


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
    )


def _datetime(hour: int) -> datetime:
    return datetime(2026, 6, 11, hour, tzinfo=timezone.utc)
