from __future__ import annotations

import pytest

from app.config import load_settings


def _set_required_environment(monkeypatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", "123:test")
    monkeypatch.setenv("BOT_USERNAME", "test_bot")


def test_role_enforcement_defaults_to_false(monkeypatch) -> None:
    _set_required_environment(monkeypatch)
    monkeypatch.delenv("ROLE_ENFORCEMENT_ENABLED", raising=False)

    assert load_settings().role_enforcement_enabled is False


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("true", True),
        ("1", True),
        ("YES", True),
        ("on", True),
        ("false", False),
        ("0", False),
        ("NO", False),
        ("off", False),
    ],
)
def test_role_enforcement_parses_explicit_boolean_values(
    monkeypatch,
    value: str,
    expected: bool,
) -> None:
    _set_required_environment(monkeypatch)
    monkeypatch.setenv("ROLE_ENFORCEMENT_ENABLED", value)

    assert load_settings().role_enforcement_enabled is expected


def test_role_enforcement_rejects_unknown_value(monkeypatch) -> None:
    _set_required_environment(monkeypatch)
    monkeypatch.setenv("ROLE_ENFORCEMENT_ENABLED", "sometimes")

    with pytest.raises(RuntimeError, match="ROLE_ENFORCEMENT_ENABLED must be one of"):
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
    monkeypatch.delenv("HEALTHCHECK_INTERVAL_MINUTES", raising=False)

    settings = load_settings()

    assert settings.healthcheck_chat_id is None
    assert settings.healthcheck_interval_minutes == 360


def test_healthcheck_notifications_parse_chat_and_interval(monkeypatch) -> None:
    _set_required_environment(monkeypatch)
    monkeypatch.setenv("HEALTHCHECK_CHAT_ID", "-1001234567890")
    monkeypatch.setenv("HEALTHCHECK_INTERVAL_MINUTES", "120")

    settings = load_settings()

    assert settings.healthcheck_chat_id == -1001234567890
    assert settings.healthcheck_interval_minutes == 120


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("HEALTHCHECK_CHAT_ID", "invalid"),
        ("HEALTHCHECK_CHAT_ID", "0"),
        ("HEALTHCHECK_INTERVAL_MINUTES", "invalid"),
        ("HEALTHCHECK_INTERVAL_MINUTES", "0"),
        ("HEALTHCHECK_INTERVAL_MINUTES", "-1"),
    ],
)
def test_healthcheck_notifications_reject_invalid_values(
    monkeypatch,
    name: str,
    value: str,
) -> None:
    _set_required_environment(monkeypatch)
    monkeypatch.delenv("HEALTHCHECK_CHAT_ID", raising=False)
    monkeypatch.delenv("HEALTHCHECK_INTERVAL_MINUTES", raising=False)
    monkeypatch.setenv(name, value)

    with pytest.raises(RuntimeError, match=name):
        load_settings()
