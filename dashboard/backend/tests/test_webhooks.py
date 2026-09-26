"""Angel One postback: signature, idempotency, fill ingestion."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from qsdash.config import settings
from qsdash.db import now_ist
from qsdash.models import FillRow, OrderRow, WebhookEvent


def _signed(client, payload: dict):
    raw = json.dumps(payload).encode()
    sig = hmac.new(settings.angel_webhook_secret.encode(), raw,
                   hashlib.sha256).hexdigest()
    return client.post("/api/webhooks/angelone", content=raw,
                       headers={"X-QS-Signature": sig,
                                "Content-Type": "application/json"})


def _order(db, coid: str, boid: str, qty: int, side: str = "BUY") -> OrderRow:
    o = OrderRow(client_order_id=coid, ts=now_ist(), mode="live",
                 symbol="NIFTY-FUT", side=side, qty=qty, ref_price=100.0,
                 status="SUBMITTED", broker_order_id=boid)
    db.add(o)
    db.commit()
    return o


def test_bad_signature_rejected(client):
    r = client.post("/api/webhooks/angelone", json={"orderid": "X1"},
                    headers={"X-QS-Signature": "deadbeef"})
    assert r.status_code == 401


def test_unknown_order_recorded_and_flagged(client, db):
    r = _signed(client, {"orderid": "UNKNOWN-1", "status": "complete",
                         "filledshares": 10, "averageprice": 101.5})
    assert r.status_code == 200
    assert r.json()["matched"] is False
    ev = (db.query(WebhookEvent)
          .filter(WebhookEvent.external_id.like("UNKNOWN-1%")).first())
    assert ev is not None and ev.status == "ignored"


def test_postback_updates_order_and_creates_fill(client, db):
    o = _order(db, "QS-T1", "BRK-77", 65)
    r = _signed(client, {"orderid": "BRK-77", "status": "complete",
                         "filledshares": 65, "averageprice": 100.4})
    assert r.status_code == 200 and r.json()["matched"] is True
    db.refresh(o)
    assert o.status == "FILLED" and o.filled_qty == 65
    fill = db.query(FillRow).filter(FillRow.order_id == o.id).first()
    assert fill is not None and fill.qty == 65 and fill.price == 100.4
    assert abs(fill.slippage - 0.4) < 1e-9


def test_duplicate_postback_is_idempotent(client, db):
    o = _order(db, "QS-T2", "BRK-78", 65)
    payload = {"orderid": "BRK-78", "status": "complete",
               "filledshares": 65, "averageprice": 100.4}
    assert _signed(client, payload).json()["matched"] is True
    r = _signed(client, payload)
    assert r.json().get("duplicate") is True
    fills = db.query(FillRow).filter(FillRow.order_id == o.id).all()
    assert len(fills) == 1  # second delivery did not double-book


# ------------------------------------------------------ 1: fail closed
def test_unset_secret_refuses_every_postback(client, db, monkeypatch):
    """With no secret configured the endpoint used to accept unsigned POSTs
    and mark orders FILLED at whatever price the body claimed."""
    o = _order(db, "QS-OPEN1", "BRK-OPEN1", 10)
    monkeypatch.setattr(settings, "angel_webhook_secret", "")
    r = client.post("/api/webhooks/angelone",
                    json={"orderid": "BRK-OPEN1", "status": "complete",
                          "filledshares": 10, "averageprice": 1.0})
    assert r.status_code == 503
    assert r.json()["detail"] == "postback disabled: no secret configured"
    db.refresh(o)
    assert o.status == "SUBMITTED" and o.filled_qty == 0
    assert db.query(FillRow).filter(FillRow.order_id == o.id).count() == 0


def test_unsigned_postback_needs_the_flag_and_a_dev_env(client, monkeypatch):
    body = {"orderid": "UNSIGNED-1", "status": "open"}
    monkeypatch.setattr(settings, "angel_webhook_secret", "")
    monkeypatch.setattr(settings, "angel_webhook_allow_unsigned", True)
    monkeypatch.setattr(settings, "env", "prod")
    assert client.post("/api/webhooks/angelone", json=body).status_code == 503
    monkeypatch.setattr(settings, "env", "dev")
    assert client.post("/api/webhooks/angelone", json=body).status_code == 200
    monkeypatch.setattr(settings, "angel_webhook_allow_unsigned", False)
    assert client.post("/api/webhooks/angelone", json=body).status_code == 503


# ---------------------------------------------- 2: partial fill pricing
def test_partial_fills_book_the_incremental_price(client, db):
    """Angel's averageprice is cumulative; the second partial used to be
    booked at it, so booked notional no longer matched the broker's."""
    o = _order(db, "QS-P1", "BRK-P1", 30)
    assert _signed(client, {"orderid": "BRK-P1", "status": "open",
                            "filledshares": 10, "averageprice": 100.0}).status_code == 200
    assert _signed(client, {"orderid": "BRK-P1", "status": "complete",
                            "filledshares": 30, "averageprice": 101.0}).status_code == 200
    fills = [(f.qty, f.price) for f in
             db.query(FillRow).filter(FillRow.order_id == o.id).order_by(FillRow.id)]
    assert fills == [(10, 100.0), (20, 101.5)]
    assert sum(q * p for q, p in fills) == pytest.approx(30 * 101.0)
    db.refresh(o)
    assert o.filled_qty == 30
    # the order row keeps Angel's cumulative average
    assert o.status_history[-1]["avg_price"] == 101.0


def test_partial_fills_on_a_sell_are_signed_and_priced(client, db):
    o = _order(db, "QS-P2", "BRK-P2", 20, side="SELL")
    _signed(client, {"orderid": "BRK-P2", "status": "open",
                     "filledshares": 5, "averageprice": 200.0})
    _signed(client, {"orderid": "BRK-P2", "status": "complete",
                     "filledshares": 20, "averageprice": 199.0})
    fills = [(f.qty, f.price) for f in
             db.query(FillRow).filter(FillRow.order_id == o.id).order_by(FillRow.id)]
    assert fills == [(-5, 200.0), (-15, pytest.approx(198.6666666667))]


# ------------------------------- 3: postback racing its own order row
def test_postback_before_its_order_row_is_processed_on_redelivery(client, db,
                                                                  monkeypatch):
    """The early delivery's dedup row used to be committed as processed, so
    Angel's re-delivery was swallowed as a duplicate and the fill was lost."""
    from qsdash.api import webhooks

    risk: list[dict] = []
    real = webhooks._publisher.publish

    def spy(topic, data, ref=None):
        if topic == "risk":
            risk.append(data)
        real(topic, data, ref)

    monkeypatch.setattr(webhooks._publisher, "publish", spy)
    payload = {"orderid": "BRK-EARLY", "ordertag": "QS-EARLY", "status": "complete",
               "filledshares": 5, "averageprice": 200.0}
    r = _signed(client, payload)
    assert r.status_code == 200 and r.json()["matched"] is False
    assert [e["kind"] for e in risk] == ["unknown_broker_order"]

    _order(db, "QS-EARLY", "", 5)
    r = _signed(client, payload)  # Angel re-delivers
    assert r.status_code == 200
    assert r.json().get("matched") is True
    fill = db.query(FillRow).filter(FillRow.client_order_id == "QS-EARLY").one()
    assert (fill.qty, fill.price) == (5, 200.0)
