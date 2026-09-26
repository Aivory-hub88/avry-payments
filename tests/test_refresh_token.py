"""Backend refresh tokens must not authenticate here (they share JWT_SECRET).

A refresh token lives 30 days and keeps verifying after logout, so before
this check it worked as a bearer credential on every require_auth route.
"""
import asyncio
import time

import jwt
import pytest
from fastapi import HTTPException

import app.auth as auth

SECRET = "test-backend-secret"


@pytest.fixture(autouse=True)
def secrets(monkeypatch):
    monkeypatch.setattr(auth, "JWT_SECRET", SECRET)
    monkeypatch.setattr(auth, "SUPABASE_JWT_SECRET", None)
    monkeypatch.setattr(auth, "INTERNAL_SERVICE_TOKEN", None)


def _token(claims: dict, secret: str = SECRET) -> str:
    return jwt.encode({**claims, "exp": int(time.time()) + 3600}, secret, algorithm="HS256")


def _auth(token: str):
    return asyncio.new_event_loop().run_until_complete(auth.require_auth(f"Bearer {token}"))


def test_access_tokens_still_work():
    assert _auth(_token({"user_id": "u1", "type": "access"}))["user_id"] == "u1"
    assert _auth(_token({"user_id": "u1", "account_type": "free"}))["user_id"] == "u1"


@pytest.mark.parametrize("claims", [
    {"user_id": "u1", "session_id": "s1", "type": "refresh"},
    {"user_id": "u1", "session_id": "s1"},
])
def test_refresh_tokens_are_rejected(claims):
    with pytest.raises(HTTPException) as e:
        _auth(_token(claims))
    assert e.value.status_code == 401


def test_supabase_access_tokens_with_session_id_still_work(monkeypatch):
    monkeypatch.setattr(auth, "SUPABASE_JWT_SECRET", "test-supabase-secret")
    t = _token({"sub": "u1", "session_id": "sb-session", "aud": "authenticated"}, "test-supabase-secret")
    assert _auth(t)["sub"] == "u1"
