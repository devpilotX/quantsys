"""The Broker interface and its value types.

Contract notes:
- `place` is idempotent on `client_order_id`: re-submitting a known id returns
  the existing ack, never a duplicate order. Callers generate stable ids.
- A send whose outcome is unknown raises OrderStateUnknown. The order may be
  live at the broker, so nothing may resend it until a lookup by
  client_order_id (the ordertag) has resolved it.
- Quantities are signed instrument units everywhere (engine convention).
- All methods may raise BrokerError; the live runner treats any error as
  "reduce risk / do nothing", never "retry blindly into the market".
- A read that fails raises; it never returns an empty result, because an
  empty position list and an unreadable one lead to opposite decisions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol

from quantsys.core.types import ExecutionStyle, Instrument, Urgency


class OrderStatus(str, Enum):
    NEW = "NEW"
    SUBMITTED = "SUBMITTED"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    # Terminal. With filled_qty > 0 this is a finished partial fill.
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    # The broker reported a state we do not recognise. Not terminal, so it is
    # never read as "done"; and the order exists at the broker, so it is never
    # safe to send again.
    UNKNOWN = "UNKNOWN"

    @property
    def terminal(self) -> bool:
        return self in (OrderStatus.FILLED, OrderStatus.CANCELLED,
                        OrderStatus.REJECTED)


class BrokerError(Exception):
    """Any broker-side failure. Carries whether a retry is safe."""

    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class OrderStateUnknown(BrokerError):
    """A send may have reached the broker but its outcome could not be
    established (response lost and the order book unreadable). Never
    retryable: resolve it by looking the order up first."""

    def __init__(self, message: str):
        super().__init__(message, retryable=False)


@dataclass
class BrokerOrder:
    client_order_id: str
    symbol: str
    side: str                 # BUY / SELL
    qty: int                  # absolute units requested
    style: ExecutionStyle
    urgency: Urgency
    limit_price: float | None = None
    broker_order_id: str = ""
    status: OrderStatus = OrderStatus.NEW
    filled_qty: int = 0
    avg_fill_price: float = 0.0
    reject_reason: str = ""
    strategy: str = ""
    ts: datetime | None = None
    # Set by the position-aware caller when the order can only shrink an
    # existing position. The Angel adapter sends a cash-equity SELL only with
    # this set: without it the sell could open a short that cannot be carried.
    reduce_only: bool = False


@dataclass
class OrderAck:
    client_order_id: str
    broker_order_id: str
    status: OrderStatus
    detail: str = ""


@dataclass
class BrokerPosition:
    symbol: str
    qty: int                  # signed
    avg_price: float = 0.0


@dataclass
class TickerQuote:
    symbol: str
    ltp: float
    ts: datetime
    bid: float = 0.0
    ask: float = 0.0
    volume: float = 0.0


class Broker(Protocol):
    """Everything the live runner needs from a venue."""

    def connect(self) -> None: ...
    def is_connected(self) -> bool: ...
    def funds(self) -> float:
        """Available trading capital (net) in INR."""
        ...

    def instruments(self) -> dict[str, Instrument]:
        """Tradeable universe with live lot/tick/margin from the master."""
        ...

    def positions(self) -> list[BrokerPosition]:
        """Broker's view of holdings — ground truth for reconciliation."""
        ...

    def open_orders(self) -> list[BrokerOrder]: ...

    def place(self, order: BrokerOrder) -> OrderAck:
        """Idempotent on order.client_order_id."""
        ...

    def cancel(self, client_order_id: str) -> OrderAck:
        """Requests a cancel. The ack is not a confirmation: poll
        order_status until the order is terminal."""
        ...

    def order_status(self, client_order_id: str) -> BrokerOrder | None: ...

    def find_by_tag(self, client_order_id: str) -> BrokerOrder | None:
        """Look an order up in the broker's book by client_order_id. None
        means "not in the book"; an unreadable book raises BrokerError."""
        ...

    def register_order(self, client_order_id: str, broker_order_id: str) -> None:
        """Restore a client -> broker id mapping known from a journal, so
        cancel and order_status work after a restart."""
        ...
