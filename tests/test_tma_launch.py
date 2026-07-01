from __future__ import annotations

import pytest

from app.tma_launch import (
    TmaLaunchTokenError,
    create_tma_launch_token,
    validate_tma_launch_token,
)


SECRET = "test-secret"
NOW = 1_800_000_000


def test_launch_token_round_trip() -> None:
    token = create_tma_launch_token(
        chat_id=-1001234567890,
        chat_type="supergroup",
        chat_title="Футбольные прогнозы",
        secret=SECRET,
        now=NOW,
    )

    context = validate_tma_launch_token(
        token,
        secret=SECRET,
        now=NOW + 60,
    )

    assert context.chat_id == -1001234567890
    assert context.chat_type == "supergroup"
    assert context.chat_title == "Футбольные прогнозы"


def test_launch_token_works_without_chat_title() -> None:
    token = create_tma_launch_token(
        chat_id=123456789,
        chat_type="private",
        secret=SECRET,
        now=NOW,
    )

    context = validate_tma_launch_token(
        token,
        secret=SECRET,
        now=NOW,
    )

    assert context.chat_id == 123456789
    assert context.chat_type == "private"
    assert context.chat_title is None


def test_launch_token_rejects_wrong_secret() -> None:
    token = create_tma_launch_token(
        chat_id=-1001234567890,
        chat_type="supergroup",
        secret=SECRET,
        now=NOW,
    )

    with pytest.raises(TmaLaunchTokenError, match="invalid"):
        validate_tma_launch_token(
            token,
            secret="another-secret",
            now=NOW,
        )


def test_launch_token_rejects_expired_token() -> None:
    token = create_tma_launch_token(
        chat_id=-1001234567890,
        chat_type="supergroup",
        secret=SECRET,
        now=NOW,
        max_age_seconds=60,
    )

    with pytest.raises(TmaLaunchTokenError, match="expired"):
        validate_tma_launch_token(
            token,
            secret=SECRET,
            now=NOW + 61,
        )


def test_launch_token_rejects_tampered_value() -> None:
    token = create_tma_launch_token(
        chat_id=-1001234567890,
        chat_type="supergroup",
        secret=SECRET,
        now=NOW,
    )
    tampered_token = f"{token[:-1]}{'A' if token[-1] != 'A' else 'B'}"

    with pytest.raises(TmaLaunchTokenError, match="invalid"):
        validate_tma_launch_token(
            tampered_token,
            secret=SECRET,
            now=NOW,
        )
