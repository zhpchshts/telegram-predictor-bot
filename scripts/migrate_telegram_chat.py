from __future__ import annotations

import argparse
from pathlib import Path

from app.chat_migration_service import migrate_telegram_chat
from app.database import initialize_database


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Move all data for a migrated Telegram group to its new chat id "
            "and revoke launch links for the old chat id."
        )
    )
    parser.add_argument("database_path", type=Path)
    parser.add_argument("old_telegram_chat_id", type=int)
    parser.add_argument("new_telegram_chat_id", type=int)
    parser.add_argument("--title")
    arguments = parser.parse_args()

    if not arguments.database_path.is_file():
        raise RuntimeError(f"Database file does not exist: {arguments.database_path}")

    initialize_database(arguments.database_path)
    result = migrate_telegram_chat(
        database_path=arguments.database_path,
        old_telegram_chat_id=arguments.old_telegram_chat_id,
        new_telegram_chat_id=arguments.new_telegram_chat_id,
        new_chat_title=arguments.title,
    )
    status = "already migrated" if result.already_migrated else "migrated"
    print(
        f"Telegram chat {status}: "
        f"local_chat_id={result.chat_id}, "
        f"old_telegram_chat_id={result.old_telegram_chat_id}, "
        f"new_telegram_chat_id={result.new_telegram_chat_id}, "
        "migrated_audit_events="
        f"{result.migrated_audit_event_count}"
    )


if __name__ == "__main__":
    main()
