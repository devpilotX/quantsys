"""Inbound webhooks. Angel One order postback:

- authenticity: HMAC-SHA256 of the raw body with ANGEL_WEBHOOK_SECRET in the
  ``X-QS-Signature`` header (hex). Angel One's native postback doesn't sign,
  so on the VPS nginx exposes this endpoint only on a secret path and the
  engine's postback registration includes that path + the relay adds the HMAC.
  Fails closed: with no secret configured every postback is refused (503),
  because a postback books fills. The only exception is the explicit
  dev-only flag angel_webhook_allow_unsigned, honoured only when env == "dev".
- idempotency: (source, external_id) unique in webhook_events; duplicates are
  acknowledged but not re-processed. A postback for an order we do not know
  yet (it can race its own order row) does not consume its dedup key, so
  Angel's re-delivery is processed.
- effect: order status/fill update persisted, reconciled against our order
  by broker_order_id / client order id, event published. averageprice is
  Angel's cumulative average: each new fill is booked at the incremental
  price of the shares it adds.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from qsdash.bus import make_sync_publisher
from qsdash.config import settings
from qsdash.db import SessionLocal, now_ist
from qsdash.models import FillRow, OrderRow, WebhookEvent

log = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks", tags=["webhooks"])

_publisher = make_sync_publisher(SessionLocal)

# Angel One postback status vocabulary -> internal lifecycle
_STATUS_MAP = {
    "open": "SUBMITTED",
    "pending": "SUBMITTED",
    "trigger pending": "SUBMITTED",
    "complete": "FILLED",
    "executed": "FILLED",
    "partially executed": "PARTIAL",
    "cancelled": "CANCELLED",
    "rejected": "REJECTED",
}


def _verify_signature(raw: bytes, signature: str) -> None:
    """Raises unless the body is authentic. No secret means no postbacks."""
    secret = settings.angel_webhook_secret
    if not secret:
        if settings.angel_webhook_allow_unsigned and settings.env == "dev":
            log.warning("accepting an UNSIGNED postback: angel_webhook_allow_unsigned "
                        "is on (dev only)")
            return
        raise HTTPException(503, "postback disabled: no secret configured")
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    # bytes, so a non-ASCII header is a mismatch rather than a TypeError
    if not hmac.compare_digest(expected.encode(), (signature or "").encode()):
        raise HTTPException(401, "bad signature")


def _increment_price(db, order: OrderRow, filled: int, avg_px: float,
                     new_qty: int) -> float:
    """Price of the ``new_qty`` shares this postback adds. Angel reports the
    cumulative average over all ``filled`` shares, so the increment traded at
    (avg * filled - prev_avg * prev_filled) / new_qty, with prev_avg the
    average of the fills already booked for this order."""
    prev_filled = order.filled_qty or 0
    if prev_filled <= 0:
        return avg_px
    qty_booked, notional_booked = (
        db.query(func.sum(func.abs(FillRow.qty)),
                 func.sum(func.abs(FillRow.qty) * FillRow.price))
        .filter(FillRow.order_id == order.id).one())
    prev_avg = notional_booked / qty_booked if qty_booked else avg_px
    return (avg_px * filled - prev_avg * prev_filled) / new_qty


@router.post("/angelone")
async def angelone_postback(request: Request):
    raw = await request.body()
    _verify_signature(raw, request.headers.get("X-QS-Signature", ""))
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(400, "invalid JSON")

    ext_id = str(
        payload.get("orderid")
        or payload.get("norenordno")
        or payload.get("uniqueorderid")
        or ""
    )
    if not ext_id:
        raise HTTPException(400, "no order id in payload")
    # status changes share an order id; key on (id, status, fill qty) so each
    # lifecycle step processes exactly once
    dedup_key = f"{ext_id}:{payload.get('status', '')}:{payload.get('filledshares', '')}"

    db = SessionLocal()
    try:
        ev = WebhookEvent(source="angelone", external_id=dedup_key, payload=payload)
        db.add(ev)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            return {"ok": True, "duplicate": True}

        order = (
            db.query(OrderRow)
            .filter(
                (OrderRow.broker_order_id == ext_id)
                | (OrderRow.client_order_id == str(payload.get("ordertag", "")))
            )
            .first()
        )
        raw_status = str(payload.get("status", "")).lower()
        mapped = _STATUS_MAP.get(raw_status)

        if order is None:
            # An order we don't know is a reconciliation red flag, not noise.
            # It can also be a postback racing its own order row, so release
            # the dedup key (a re-delivery must be processed, not swallowed as
            # a duplicate) and keep the forensic row under a key of its own.
            db.rollback()
            db.add(WebhookEvent(
                source="angelone",
                external_id=f"{dedup_key}:unmatched:{now_ist().timestamp()}",
                payload=payload, status="ignored", error="no matching internal order"))
            db.commit()
            _publisher.publish("risk", {
                "kind": "unknown_broker_order", "broker_order_id": ext_id,
                "status": raw_status,
            })
            return {"ok": True, "matched": False}

        filled = int(float(payload.get("filledshares", 0) or 0))
        avg_px = float(payload.get("averageprice", 0) or 0)
        if not math.isfinite(avg_px):
            raise ValueError(f"averageprice {payload.get('averageprice')!r} is not finite")

        order.broker_order_id = ext_id
        hist = list(order.status_history or [])
        # the order row keeps Angel's cumulative figures; fills carry increments
        hist.append({"ts": now_ist().isoformat(), "status": raw_status, "via": "postback",
                     "filled": filled, "avg_price": avg_px})
        order.status_history = hist
        order.updated_at = now_ist()
        if mapped:
            order.status = mapped
        if mapped == "REJECTED":
            order.reject_reason = str(payload.get("text", "") or payload.get("message", ""))

        new_qty = filled - order.filled_qty
        if new_qty > 0 and avg_px > 0:
            px = _increment_price(db, order, filled, avg_px, new_qty)
            if not (math.isfinite(px) and px > 0):
                # inconsistent cumulative averages: keep the quantity right
                # (the book is what reconciles) and flag the price
                log.error("postback %s: incremental price %r is not positive; "
                          "booking %d at the cumulative %.4f", ext_id, px, new_qty, avg_px)
                _publisher.publish("risk", {
                    "kind": "fill_price_inconsistent", "broker_order_id": ext_id,
                    "incremental_price": px, "average_price": avg_px})
                px = avg_px
            signed = new_qty if order.side == "BUY" else -new_qty
            db.add(FillRow(
                order_id=order.id, client_order_id=order.client_order_id,
                ts=now_ist(), mode=order.mode, symbol=order.symbol,
                strategy=order.strategy, qty=signed, price=px,
                slippage=(px - order.ref_price) * (1 if order.side == "BUY" else -1)
                if order.ref_price else 0.0,
            ))
            order.filled_qty = filled

        ev.status = "processed"
        ev.processed_at = now_ist()
        db.commit()
        _publisher.publish("orders", {
            "id": order.id, "client_order_id": order.client_order_id,
            "symbol": order.symbol, "status": order.status,
            "filled_qty": order.filled_qty, "avg_price": avg_px, "via": "postback",
        })
        return {"ok": True, "matched": True, "status": order.status}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        log.exception("postback processing failed")
        # persist the failure for forensics; never silently drop
        try:
            db.add(WebhookEvent(source="angelone", external_id=f"err:{dedup_key}:{now_ist().timestamp()}",
                                payload=payload, status="error", error=str(e)))
            db.commit()
        except Exception:
            db.rollback()
        raise HTTPException(500, "processing error")
    finally:
        db.close()
