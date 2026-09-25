"""Wipe TRADING data (decisions/orders/fills/positions/equity/...) for a clean
dev session. Auth, audit, config and command tables are preserved.
NEVER run against a live-mode database with real history you care about.
"""

from qsdash.db import SessionLocal
from sqlalchemy import text

TABLES = [
    "fills", "orders", "positions", "decisions", "equity_curve",
    "pnl_attribution", "risk_events", "regime_history", "market_bars",
    "strategy_stats",
]

s = SessionLocal()
s.execute(text(
    "TRUNCATE " + ", ".join(TABLES) + " RESTART IDENTITY CASCADE"
))
s.commit()
s.close()
print("trading tables truncated:", ", ".join(TABLES))
