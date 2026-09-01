from __future__ import annotations

from pathlib import Path

import pytest

from app.config import load_settings


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _set_required_environment(monkeypatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", "123:test")
    monkeypatch.setenv("BOT_USERNAME", "test_bot")


def test_telegram_network_settings_default_to_bounded_direct_access(
    monkeypatch,
) -> None:
    _set_required_environment(monkeypatch)
    monkeypatch.delenv("TELEGRAM_ADMIN_CHECK_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_API_FALLBACK_IPS", raising=False)

    settings = load_settings()

    assert settings.telegram_admin_check_timeout_seconds == 3.0
    assert settings.telegram_bot_api_fallback_ips == ()


def test_telegram_network_settings_accept_fallback_ips(monkeypatch) -> None:
    _set_required_environment(monkeypatch)
    monkeypatch.setenv("TELEGRAM_ADMIN_CHECK_TIMEOUT_SECONDS", "1.5")
    monkeypatch.setenv(
        "TELEGRAM_BOT_API_FALLBACK_IPS",
        " 149.154.167.220, 2001:67c:4e8:f004::9,149.154.167.220 ",
    )

    settings = load_settings()

    assert settings.telegram_admin_check_timeout_seconds == 1.5
    assert settings.telegram_bot_api_fallback_ips == (
        "149.154.167.220",
        "2001:67c:4e8:f004::9",
    )


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("TELEGRAM_ADMIN_CHECK_TIMEOUT_SECONDS", "0"),
        ("TELEGRAM_ADMIN_CHECK_TIMEOUT_SECONDS", "invalid"),
        ("TELEGRAM_BOT_API_FALLBACK_IPS", "not-an-ip"),
        ("TELEGRAM_BOT_API_FALLBACK_IPS", "127.0.0.1"),
        ("TELEGRAM_BOT_API_FALLBACK_IPS", "149.154.167.220,"),
    ],
)
def test_telegram_network_settings_reject_invalid_values(
    monkeypatch,
    name: str,
    value: str,
) -> None:
    _set_required_environment(monkeypatch)
    monkeypatch.delenv("TELEGRAM_ADMIN_CHECK_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_API_FALLBACK_IPS", raising=False)
    monkeypatch.setenv(name, value)

    with pytest.raises(RuntimeError, match=name):
        load_settings()


def test_mtproto_configuration_is_optional(monkeypatch) -> None:
    _set_required_environment(monkeypatch)
    monkeypatch.delenv("TELEGRAM_API_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_API_HASH", raising=False)
    monkeypatch.delenv("TELEGRAM_MTPROTO_SESSION_PATH", raising=False)

    settings = load_settings()

    assert settings.telegram_api_id is None
    assert settings.telegram_api_hash is None
    assert settings.telegram_mtproto_session_path.name == "telegram-mtproto"


def test_invalid_mtproto_api_id_does_not_break_settings(monkeypatch) -> None:
    _set_required_environment(monkeypatch)
    monkeypatch.setenv("TELEGRAM_API_ID", "invalid")
    monkeypatch.setenv("TELEGRAM_API_HASH", "secret-hash")

    settings = load_settings()

    assert settings.telegram_api_id is None
    assert settings.telegram_api_hash == "secret-hash"


def test_healthcheck_notifications_default_to_disabled(monkeypatch) -> None:
    _set_required_environment(monkeypatch)
    monkeypatch.delenv("HEALTHCHECK_CHAT_ID", raising=False)

    settings = load_settings()

    assert settings.healthcheck_chat_id is None


def test_healthcheck_notifications_parse_chat(monkeypatch) -> None:
    _set_required_environment(monkeypatch)
    monkeypatch.setenv("HEALTHCHECK_CHAT_ID", "-1001234567890")

    settings = load_settings()

    assert settings.healthcheck_chat_id == -1001234567890


def test_shared_tournament_admin_ids_are_optional_and_deduplicated(monkeypatch) -> None:
    _set_required_environment(monkeypatch)
    monkeypatch.setenv("SHARED_TOURNAMENT_ADMIN_IDS", "123, 456,123")

    assert load_settings().shared_tournament_admin_ids == frozenset({123, 456})


def test_champions_league_sync_defaults_to_idle_2026_season(monkeypatch) -> None:
    _set_required_environment(monkeypatch)
    monkeypatch.delenv("FOOTBALL_DATA_API_TOKEN", raising=False)
    monkeypatch.delenv("CHAMPIONS_LEAGUE_SYNC_SEASON", raising=False)
    monkeypatch.delenv("CHAMPIONS_LEAGUE_SYNC_INTERVAL_MINUTES", raising=False)

    settings = load_settings()

    assert settings.football_data_api_token is None
    assert settings.champions_league_sync_season == 2026
    assert settings.champions_league_sync_interval_minutes == 10


def test_champions_league_sync_configuration_is_parsed(monkeypatch) -> None:
    _set_required_environment(monkeypatch)
    monkeypatch.setenv("FOOTBALL_DATA_API_TOKEN", " secret-token ")
    monkeypatch.setenv("CHAMPIONS_LEAGUE_SYNC_SEASON", "2026")
    monkeypatch.setenv("CHAMPIONS_LEAGUE_SYNC_INTERVAL_MINUTES", "15")

    settings = load_settings()

    assert settings.football_data_api_token == "secret-token"
    assert settings.champions_league_sync_season == 2026
    assert settings.champions_league_sync_interval_minutes == 15


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("CHAMPIONS_LEAGUE_SYNC_SEASON", "1999"),
        ("CHAMPIONS_LEAGUE_SYNC_SEASON", "2027"),
        ("CHAMPIONS_LEAGUE_SYNC_SEASON", "invalid"),
        ("CHAMPIONS_LEAGUE_SYNC_INTERVAL_MINUTES", "0"),
        ("CHAMPIONS_LEAGUE_SYNC_INTERVAL_MINUTES", "1441"),
    ],
)
def test_champions_league_sync_rejects_invalid_numbers(
    monkeypatch,
    name: str,
    value: str,
) -> None:
    _set_required_environment(monkeypatch)
    monkeypatch.setenv(name, value)

    with pytest.raises(RuntimeError, match=name):
        load_settings()


@pytest.mark.parametrize("value", ["0", "-1", "abc", "123,,456"])
def test_shared_tournament_admin_ids_reject_invalid_values(
    monkeypatch, value: str
) -> None:
    _set_required_environment(monkeypatch)
    monkeypatch.setenv("SHARED_TOURNAMENT_ADMIN_IDS", value)

    with pytest.raises(RuntimeError, match="SHARED_TOURNAMENT_ADMIN_IDS"):
        load_settings()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("HEALTHCHECK_CHAT_ID", "invalid"),
        ("HEALTHCHECK_CHAT_ID", "0"),
    ],
)
def test_healthcheck_notifications_reject_invalid_values(
    monkeypatch,
    name: str,
    value: str,
) -> None:
    _set_required_environment(monkeypatch)
    monkeypatch.delenv("HEALTHCHECK_CHAT_ID", raising=False)
    monkeypatch.setenv(name, value)

    with pytest.raises(RuntimeError, match=name):
        load_settings()


def test_docker_build_context_excludes_runtime_secrets_and_database_sidecars() -> None:
    patterns = {
        line.strip()
        for line in (PROJECT_ROOT / ".dockerignore")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert {
        ".env",
        ".env.*",
        "data/",
        "*.db",
        "*.db-*",
        "*.sqlite3",
        "*.sqlite3-*",
        "*.session",
        "*.session-*",
    } <= patterns
