"""
Durable order intent log.

An order is written to disk BEFORE it is transmitted to the broker, and updated
once the outcome is known. If the process dies between those two points, the
next startup finds an intent with no recorded outcome and can ask the broker
what actually happened instead of silently forgetting the order.

This closes the window that position-level reconciliation alone cannot: a fill
that arrives after the client stopped waiting, or a transmission whose outcome
was never observed. Without it there is no way to distinguish "never sent" from
"sent, outcome unknown" - and those require opposite recovery actions.

Append-only JSONL: a crash mid-write can corrupt at most the last line, never
the history.
"""

import json
import logging
import os
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

PENDING = "PENDING"
SENT = "SENT"
RESOLVED = "RESOLVED"
ORPHANED = "ORPHANED"
ABANDONED = "ABANDONED"


@dataclass
class OrderIntent:
    """One decision to send an order, and what became of it."""
    intent_id: str
    created_at: str
    symbol: str
    action: str
    quantity: int
    reason: str = ""
    state: str = PENDING

    sent_at: str = ""
    order_id: Optional[int] = None
    perm_id: Optional[int] = None

    resolved_at: str = ""
    final_status: str = ""
    filled_qty: int = 0
    fill_price: float = 0.0
    note: str = ""

    @property
    def unresolved(self) -> bool:
        return self.state in (PENDING, SENT, ORPHANED)

    def describe(self) -> str:
        return (f"{self.intent_id[:8]} {self.action} {self.quantity} {self.symbol}"
                f" [{self.state}]" + (f" reason={self.reason}" if self.reason else ""))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class IntentLog:
    """Append-only log of order intentions."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._open: Dict[str, OrderIntent] = {}

    def _needs_newline(self) -> bool:
        """True if the file ends mid-line - a torn write from a hard kill."""
        try:
            if not self.path.exists() or self.path.stat().st_size == 0:
                return False
            with self.path.open("rb") as f:
                f.seek(-1, os.SEEK_END)
                return f.read(1) != b"\n"
        except OSError:
            return False

    def _append(self, intent: OrderIntent) -> None:
        """
        Write one record and force it to disk before returning.

        If the previous write was torn off mid-line, terminate it first -
        otherwise this record glues onto the fragment and both become
        unreadable, losing a good record along with the damaged one.
        """
        line = json.dumps(asdict(intent), ensure_ascii=False)
        prefix = "\n" if self._needs_newline() else ""
        with self.path.open("a", encoding="utf-8") as f:
            f.write(prefix + line + "\n")
            f.flush()
            os.fsync(f.fileno())

    def record(self, symbol: str, action: str, quantity: int,
               reason: str = "") -> OrderIntent:
        """Persist the intention. Call this BEFORE touching the broker."""
        intent = OrderIntent(
            intent_id=uuid.uuid4().hex,
            created_at=_now(),
            symbol=symbol,
            action=action,
            quantity=int(quantity),
            reason=reason,
            state=PENDING,
        )
        self._append(intent)
        self._open[intent.intent_id] = intent
        return intent

    def mark_sent(self, intent: OrderIntent, order_id=None, perm_id=None) -> None:
        """Transmission happened. Outcome still unknown."""
        intent.state = SENT
        intent.sent_at = _now()
        if order_id is not None:
            intent.order_id = int(order_id)
        if perm_id is not None:
            intent.perm_id = int(perm_id)
        self._append(intent)

    def resolve(self, intent: OrderIntent, status: str, filled_qty: int = 0,
                fill_price: float = 0.0, note: str = "", perm_id=None) -> None:
        """Broker gave a terminal answer."""
        intent.state = RESOLVED
        intent.resolved_at = _now()
        intent.final_status = status
        intent.filled_qty = int(filled_qty or 0)
        intent.fill_price = float(fill_price or 0.0)
        intent.note = note
        if perm_id is not None:
            intent.perm_id = int(perm_id)
        self._append(intent)
        self._open.pop(intent.intent_id, None)

    def abandon(self, intent: OrderIntent, note: str = "") -> None:
        """Verified against the broker: this order never filled."""
        intent.state = ABANDONED
        intent.resolved_at = _now()
        intent.note = note
        self._append(intent)
        self._open.pop(intent.intent_id, None)

    def _replay(self) -> Dict[str, OrderIntent]:
        """Rebuild final state per intent_id. Last record for an id wins."""
        latest: Dict[str, OrderIntent] = {}
        if not self.path.exists():
            return latest
        with self.path.open(encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    latest[data["intent_id"]] = OrderIntent(**data)
                except Exception as e:
                    logger.warning("intent log: skipping bad line %d: %s", lineno, e)
        return latest

    def find_unresolved(self) -> List[OrderIntent]:
        """Intents with no terminal outcome - an order may have been sent."""
        return [i for i in self._replay().values() if i.unresolved]

    def mark_orphaned(self, intent: OrderIntent) -> None:
        """Flag an intent found unresolved at startup, pending verification."""
        intent.state = ORPHANED
        self._append(intent)
        self._open[intent.intent_id] = intent

    def open_intents(self) -> List[OrderIntent]:
        """Unresolved intents from the current process."""
        return list(self._open.values())

    def stats(self) -> dict:
        by_state: Dict[str, int] = {}
        for i in self._replay().values():
            by_state[i.state] = by_state.get(i.state, 0) + 1
        return by_state
