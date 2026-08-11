from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from aiogram.types import InputRichMessage

from app.audit_service import AuditActor, AuditActorRole
from app.contest_publications import render_publication_messages
from app.contest_service import (
    create_world_cup_2026_contest,
    get_contest_details,
    save_match_prediction_publication_settings,
    save_swiss_stage_prediction,
    save_swiss_stage_prediction_settings,
    save_swiss_stage_result,
    save_tournament_teams,
)
from app.database import database_connection, initialize_database
from app.publication_outbox import ClaimedPublication
from app.publication_worker import process_due_contest_publications


CHAT_ID = -1001234567890
ADMIN_ID = 101
DEADLINE = "2030-01-01T12:00:00Z"
OPEN_TIME = datetime(2029, 1, 1, tzinfo=timezone.utc)
CLOSED_TIME = datetime(2030, 1, 2, tzinfo=timezone.utc)
AUDIT_ACTOR = AuditActor(
    telegram_chat_id=CHAT_ID,
    telegram_user_id=ADMIN_ID,
    role=AuditActorRole.TELEGRAM_ADMIN,
)


@dataclass(frozen=True, slots=True)
class SentMessage:
    message_id: int


class RecordingBot:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.edited: list[str] = []

    async def send_rich_message(
        self,
        chat_id: int,
        *,
        rich_message: InputRichMessage,
    ) -> SentMessage:
        self.sent.append(str(rich_message.html))
        return SentMessage(message_id=1000 + len(self.sent))

    async def edit_message_text(
        self,
        *,
        chat_id: int,
        message_id: int,
        rich_message: InputRichMessage,
    ) -> bool:
        self.edited.append(str(rich_message.html))
        return True

    async def delete_message(self, chat_id: int, message_id: int) -> bool:
        return True


def test_swiss_deadline_publication_is_scheduled_and_always_statistical(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    contest_id, teams = _configured_contest(database_path)
    save_swiss_stage_prediction(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=202,
        first_name="Секретная Алиса",
        last_name=None,
        username="alice",
        direct_team_ids=[teams["Альфа"], teams["Бета"]],
        elimination_team_ids=[teams["Гамма"], teams["Дельта"]],
        now_utc=OPEN_TIME,
    )

    with database_connection(database_path) as connection:
        publication = connection.execute(
            """
            SELECT id, desired_action, reconcile_at
            FROM contest_publications
            WHERE contest_id = ? AND publication_type = 'swiss_predictions'
            """,
            (contest_id,),
        ).fetchone()
    assert publication is not None
    assert publication["desired_action"] == "withdraw"
    assert publication["reconcile_at"] == "2030-01-01T12:00:00.000000Z"

    text = "".join(
        render_publication_messages(
            database_path=database_path,
            publication=_publication(
                publication_id=int(publication["id"]),
                contest_id=contest_id,
                publication_type="swiss_predictions",
            ),
            now_utc=CLOSED_TIME,
        )
    )
    assert "Прогнозов: 1" in text
    assert "1. Альфа — 100%" in text
    assert "1. Бета — 100%" in text
    assert "1. Гамма — 100%" in text
    assert "1. Дельта — 100%" in text
    assert "5. Эпсилон — 0%" in text
    assert "Секретная Алиса" not in text
    assert "напрямую" not in text.lower()
    assert "<table" not in text


def test_swiss_result_publication_uses_route_scoring_and_is_revised(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    contest_id, teams = _configured_contest(database_path)
    save_swiss_stage_prediction(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=202,
        first_name="Секретная Алиса",
        last_name=None,
        username="alice",
        direct_team_ids=[teams["Альфа"], teams["Бета"]],
        elimination_team_ids=[teams["Гамма"], teams["Дельта"]],
        now_utc=OPEN_TIME,
    )
    save_swiss_stage_result(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        direct_team_ids=[teams["Альфа"], teams["Бета"]],
        elimination_team_ids=[teams["Гамма"], teams["Дельта"]],
        audit_actor=AUDIT_ACTOR,
        now_utc=CLOSED_TIME,
    )

    with database_connection(database_path) as connection:
        publication = connection.execute(
            """
            SELECT id, desired_revision
            FROM contest_publications
            WHERE contest_id = ? AND publication_type = 'swiss_result'
            """,
            (contest_id,),
        ).fetchone()
    assert publication is not None
    assert publication["desired_revision"] == 1

    claim = _publication(
        publication_id=int(publication["id"]),
        contest_id=contest_id,
        publication_type="swiss_result",
    )
    first_text = "".join(
        render_publication_messages(
            database_path=database_path,
            publication=claim,
            now_utc=CLOSED_TIME,
        )
    )
    assert "Прямой проход: Альфа, Бета" in first_text
    assert "Через стыки: Гамма, Дельта" in first_text
    assert "Прогнозов: 1" in first_text
    assert "Средняя точность: 100%" in first_text
    assert "Идеальный прогноз собрали: 1." in first_text
    assert "Секретная Алиса" not in first_text
    assert "<table" not in first_text

    save_swiss_stage_result(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        direct_team_ids=[teams["Альфа"], teams["Гамма"]],
        elimination_team_ids=[teams["Бета"], teams["Эпсилон"]],
        audit_actor=AUDIT_ACTOR,
        now_utc=CLOSED_TIME,
    )
    with database_connection(database_path) as connection:
        revised = connection.execute(
            """
            SELECT desired_revision
            FROM contest_publications
            WHERE contest_id = ? AND publication_type = 'swiss_result'
            """,
            (contest_id,),
        ).fetchone()
    assert revised is not None and revised["desired_revision"] == 2

    corrected_text = "".join(
        render_publication_messages(
            database_path=database_path,
            publication=claim,
            now_utc=CLOSED_TIME,
        )
    )
    assert "Прямой проход: Альфа, Гамма" in corrected_text
    assert "Через стыки: Бета, Эпсилон" in corrected_text
    assert "Средняя точность: 50%" in corrected_text


def test_swiss_publications_are_not_created_while_master_switch_is_disabled(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    contest_id = _create_contest(database_path)
    save_tournament_teams(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        team_names=["Альфа", "Бета", "Гамма", "Дельта", "Эпсилон"],
        audit_actor=AUDIT_ACTOR,
    )
    save_swiss_stage_prediction_settings(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        enabled=True,
        deadline_at=DEADLINE,
        direct_qualifier_count=2,
        elimination_qualifier_count=2,
        audit_actor=AUDIT_ACTOR,
    )

    with database_connection(database_path) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM contest_publications WHERE contest_id = ?",
            (contest_id,),
        ).fetchone()[0]
    assert count == 0


def test_swiss_publications_are_delivered_once_and_corrections_are_edited(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    contest_id, teams = _configured_contest(database_path)
    save_swiss_stage_prediction(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=202,
        first_name="Алиса",
        last_name=None,
        username="alice",
        direct_team_ids=[teams["Альфа"], teams["Бета"]],
        elimination_team_ids=[teams["Гамма"], teams["Дельта"]],
        now_utc=OPEN_TIME,
    )
    bot = RecordingBot()

    assert (
        asyncio.run(
            process_due_contest_publications(
                bot=bot,
                database_path=database_path,
                now_utc=OPEN_TIME,
            )
        )
        == 1
    )
    assert bot.sent == []
    assert (
        asyncio.run(
            process_due_contest_publications(
                bot=bot,
                database_path=database_path,
                now_utc=CLOSED_TIME,
            )
        )
        == 1
    )
    assert len(bot.sent) == 1
    assert "Прогнозы на швейцарский этап" in bot.sent[0]
    assert (
        asyncio.run(
            process_due_contest_publications(
                bot=bot,
                database_path=database_path,
                now_utc=CLOSED_TIME,
            )
        )
        == 0
    )

    save_swiss_stage_result(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        direct_team_ids=[teams["Альфа"], teams["Бета"]],
        elimination_team_ids=[teams["Гамма"], teams["Дельта"]],
        audit_actor=AUDIT_ACTOR,
        now_utc=CLOSED_TIME,
    )
    assert (
        asyncio.run(
            process_due_contest_publications(
                bot=bot,
                database_path=database_path,
                now_utc=CLOSED_TIME,
            )
        )
        == 1
    )
    assert len(bot.sent) == 2
    assert "Итоги швейцарского этапа" in bot.sent[1]

    save_swiss_stage_result(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        direct_team_ids=[teams["Альфа"], teams["Гамма"]],
        elimination_team_ids=[teams["Бета"], teams["Эпсилон"]],
        audit_actor=AUDIT_ACTOR,
        now_utc=CLOSED_TIME,
    )
    assert (
        asyncio.run(
            process_due_contest_publications(
                bot=bot,
                database_path=database_path,
                now_utc=CLOSED_TIME,
            )
        )
        == 1
    )
    assert len(bot.sent) == 2
    assert len(bot.edited) == 1
    assert "Прямой проход: Альфа, Гамма" in bot.edited[0]


def _configured_contest(database_path: Path) -> tuple[int, dict[str, int]]:
    initialize_database(database_path)
    contest_id = _create_contest(database_path)
    names = ["Альфа", "Бета", "Гамма", "Дельта", "Эпсилон"]
    save_tournament_teams(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        team_names=names,
        audit_actor=AUDIT_ACTOR,
    )
    save_match_prediction_publication_settings(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=ADMIN_ID,
        first_name="Администратор",
        last_name=None,
        username="admin",
        enabled=True,
        now_utc=OPEN_TIME,
        audit_actor=AUDIT_ACTOR,
    )
    save_swiss_stage_prediction_settings(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        enabled=True,
        deadline_at=DEADLINE,
        direct_qualifier_count=2,
        elimination_qualifier_count=2,
        audit_actor=AUDIT_ACTOR,
    )
    details = get_contest_details(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        now_utc=OPEN_TIME,
    )
    return contest_id, {
        team.name: team.id for team in details.swiss_stage_prediction.candidates
    }


def _create_contest(database_path: Path) -> int:
    return create_world_cup_2026_contest(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        chat_title="Тестовый чат",
        telegram_user_id=ADMIN_ID,
        first_name="Администратор",
        last_name=None,
        username="admin",
        contest_name="Швейцарский этап",
        idempotency_key="create-swiss-publications-contest",
        audit_actor=AUDIT_ACTOR,
    ).contest.id


def _publication(
    *,
    publication_id: int,
    contest_id: int,
    publication_type: str,
) -> ClaimedPublication:
    return ClaimedPublication(
        id=publication_id,
        contest_id=contest_id,
        publication_type=publication_type,  # type: ignore[arg-type]
        entity_id=contest_id,
        desired_revision=1,
        desired_action="publish",
        claim_token="test",
    )
