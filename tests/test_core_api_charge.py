"""Core API answers HTTP 200 even when it refuses a charge; the body decides."""
import asyncio

import httpx
import pytest

from app.services import pricing
from app.services.payment_gateway import midtrans_service


class _Resp:
    def __init__(self, body, status=200):
        self._body = body
        self.status_code = status

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("x", request=None, response=self)


@pytest.fixture
def gateway(monkeypatch):
    monkeypatch.setattr(midtrans_service, "mock_mode", False)
    monkeypatch.setattr(midtrans_service, "server_key", "Mid-server-test")
    monkeypatch.setattr(pricing, "usd_to_idr", lambda usd: int(round(usd * 16000)))
    sent = {}

    def respond(body):
        async def post(self, url, json=None, headers=None):
            sent["payload"] = json
            return _Resp(body)
        monkeypatch.setattr(httpx.AsyncClient, "post", post)

    return respond


def _charge(channel, token=None):
    return asyncio.run(midtrans_service.create_charge(
        user_id="u1", product="operational", channel=channel, card_token_id=token,
    ))


def test_channel_not_activated_is_a_failure_that_falls_back_to_snap(gateway):
    gateway({"status_code": "402", "status_message": "Payment channel is not activated."})
    for channel in ("gopay", "qris"):
        r = _charge(channel)
        assert r["success"] is False and r["fallback_to_snap"] is True
        assert r["error"] == "Payment channel is not activated."
    r = _charge("credit_card", token="tok")
    assert r["success"] is False and r["fallback_to_snap"] is True


def test_card_refused_for_another_reason_is_shown_not_rerouted(gateway):
    gateway({"status_code": "411", "status_message": "Token id is missing, invalid, or timed out"})
    r = _charge("credit_card", token="tok")
    assert r["success"] is False and r["fallback_to_snap"] is False
    assert "Token id" in r["error"]


def test_created_charge_is_a_success(gateway):
    gateway({
        "status_code": "201", "status_message": "GoPay transaction is created",
        "transaction_id": "t1", "transaction_status": "pending",
        "actions": [{"name": "generate-qr-code", "url": "https://qr"}],
    })
    r = _charge("gopay")
    assert r["success"] is True and r["transaction_id"] == "t1" and r["qr_url"] == "https://qr"
