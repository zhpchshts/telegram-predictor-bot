from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass
from typing import Any


DEFAULT_TMA_LAUNCH_TOKEN_MAX_AGE_SECONDS = 30 * 24 * 60 * 60
MAX_TMA_LAUNCH_TOKEN_LENGTH = 512
TMA_LAUNCH_TOKEN_PATTERN = re.compile(r"^[A-Z2-7]+$")


class TmaLaunchTokenError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class TmaLaunchContext:
    chat_id: int
    chat_type: str
    chat_title: str | None = None


def create_tma_launch_token(
    *,
    chat_id: int,
    chat_type: str,
    secret: str,
    chat_title: str | None = None,
    now: int | None = None,
    max_age_seconds: int = DEFAULT_TMA_LAUNCH_TOKEN_MAX_AGE_SECONDS,
) -> str:
    if isinstance(chat_id, bool) or not isinstance(chat_id, int):
        raise TmaLaunchTokenError("chat_id must be an integer.")

    if not chat_type:
        raise TmaLaunchTokenError("chat_type is required.")

    if not secret:
        raise TmaLaunchTokenError("secret is required.")

    current_time = int(time.time()) if now is None else now

    payload: dict[str, object] = {
        "chat_id": chat_id,
        "chat_type": chat_type,
        "expires_at": current_time + max_age_seconds,
    }

    if chat_title:
        payload["chat_title"] = chat_title

    token = _encode_launch_token(payload, secret=secret)

    if len(token) > MAX_TMA_LAUNCH_TOKEN_LENGTH and "chat_title" in payload:
        payload.pop("chat_title")
        token = _encode_launch_token(payload, secret=secret)

    if len(token) > MAX_TMA_LAUNCH_TOKEN_LENGTH:
        raise TmaLaunchTokenError("TMA launch token is too long.")

    return token


def validate_tma_launch_token(
    token: str,
    *,
    secret: str,
    now: int | None = None,
) -> TmaLaunchContext:
    if not secret:
        raise TmaLaunchTokenError("secret is required.")

    if not token:
        raise TmaLaunchTokenError("TMA launch token is required.")

    if len(token) > MAX_TMA_LAUNCH_TOKEN_LENGTH:
        raise TmaLaunchTokenError("TMA launch token is too long.")

    if TMA_LAUNCH_TOKEN_PATTERN.fullmatch(token) is None:
        raise TmaLaunchTokenError("TMA launch token is invalid.")

    try:
        envelope = json.loads(_base32_decode(token).decode())
    except (
        binascii.Error,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
    ) as error:
        raise TmaLaunchTokenError("TMA launch token is invalid.") from error

    if not isinstance(envelope, dict):
        raise TmaLaunchTokenError("TMA launch token is invalid.")

    payload = envelope.get("payload")
    signature = envelope.get("signature")

    if not isinstance(payload, dict) or not isinstance(signature, str):
        raise TmaLaunchTokenError("TMA launch token is invalid.")

    expected_signature = _sign_payload(payload, secret=secret)

    if not hmac.compare_digest(signature, expected_signature):
        raise TmaLaunchTokenError("TMA launch token is invalid.")

    expires_at = payload.get("expires_at")

    if isinstance(expires_at, bool) or not isinstance(expires_at, int):
        raise TmaLaunchTokenError("TMA launch token is invalid.")

    current_time = int(time.time()) if now is None else now

    if expires_at < current_time:
        raise TmaLaunchTokenError("TMA launch token is expired.")

    return _build_launch_context(payload)


def _build_launch_context(payload: dict[str, Any]) -> TmaLaunchContext:
    chat_id = payload.get("chat_id")

    if isinstance(chat_id, bool) or not isinstance(chat_id, int):
        raise TmaLaunchTokenError("TMA launch token is invalid.")

    chat_type = payload.get("chat_type")

    if not isinstance(chat_type, str) or not chat_type:
        raise TmaLaunchTokenError("TMA launch token is invalid.")

    chat_title = payload.get("chat_title")

    if chat_title is not None and not isinstance(chat_title, str):
        raise TmaLaunchTokenError("TMA launch token is invalid.")

    return TmaLaunchContext(
        chat_id=chat_id,
        chat_type=chat_type,
        chat_title=chat_title,
    )


def _sign_payload(payload: dict[str, Any], *, secret: str) -> str:
    payload_json = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()

    return _base64url_encode(
        hmac.new(
            key=secret.encode(),
            msg=payload_json,
            digestmod=hashlib.sha256,
        ).digest()
    )


def _encode_launch_token(payload: dict[str, Any], *, secret: str) -> str:
    return _base32_encode(
        json.dumps(
            {
                "payload": payload,
                "signature": _sign_payload(payload, secret=secret),
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    )


def _base64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _base32_encode(data: bytes) -> str:
    return base64.b32encode(data).decode().rstrip("=")


def _base32_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 8)
    return base64.b32decode(value + padding)
