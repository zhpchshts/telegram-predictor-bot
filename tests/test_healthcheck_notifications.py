from __future__ import annotations

import asyncio
from pathlib import Path

from app.database import initialize_database
from app.healthcheck_notifications import (
    get_healthcheck_snapshot,
    send_healthcheck_notification,
)


class FakeBot:
    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []

    async def send_message(self, chat_id: int, *, text: str) -> object:
        message = {"chat_id": chat_id, "text": text}
        self.messages.append(message)
        return message


def test_healthcheck_snapshot_counts_empty_active_state(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)

    snapshot = get_healthcheck_snapshot(database_path=database_path)

    assert snapshot.active_contests_count == 0
    assert snapshot.active_matches_count == 0
    assert snapshot.saved_predictions_count == 0


def test_healthcheck_notification_sends_status_message(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    bot = FakeBot()

    asyncio.run(
        send_healthcheck_notification(
            bot=bot,
            database_path=database_path,
            chat_id=123,
        )
    )

    assert len(bot.messages) == 1
    message = bot.messages[0]
    assert message["chat_id"] == 123
    assert "✅ Клевер работает." in message["text"]
    assert "Время сервера UTC:" in message["text"]
    assert "Активных конкурсов: 0" in message["text"]
    assert "Предстоящих и идущих матчей: 0" in message["text"]
    assert "Сохранённых прогнозов: 0" in message["text"]
