"""
Execution registry: event-driven ownership and deduplication.

Polling an order's status answers "what is true right now, as far as I bothered
to ask". Events answer "what happened, the moment it happened" - including after
the client stopped waiting. That difference is the whole point: a fill that
arrives thirty seconds after a timeout is still recorded here.

Two identifiers carry the weight:

  permId  - assigned by the broker, stable across reconnects and process
            restarts, and present on orders placed by a different client.
            orderId cannot do this: it is scoped to one client session.

  execId  - unique per execution. The broker may report the same execution more
            than once (reconnect, history replay, duplicate subscription).
            Summing quantities without deduplicating on execId double-counts
            fills, and a double-counted fill is an invented position.

Cumulative filled quantity from the broker is treated as authoritative rather
than derived by adding increments, for the same reason.
"""

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set

logger = logging.getLogger(__name__)

# IBKR order states. PendingCancel is still live: the cancel was requested,
# not confirmed, and the order can still fill. ApiPending: accepted by the
# API, not yet sent on.
LIVE = ("ApiPending", "PendingSubmit", "PreSubmitted", "Submitted", "PendingCancel")
TERMINAL = ("Filled", "Cancelled", "ApiCancelled", "Inactive")


@dataclass
class Execution:
    """One fill, as reported by the broker."""
    exec_id: str
    perm_id: Optional[int]
    order_id: Optional[int]
    symbol: str
    side: str
    shares: float
    price: float
    time: str = ""
    account: str = ""
    commission: float = 0.0
    realized_pnl: float = 0.0
    client_id: Optional[int] = None


@dataclass
class OrderRecord:
    """Live view of one order, rebuilt from events rather than polled."""
    perm_id: Optional[int]
    order_id: Optional[int]
    symbol: str
    action: str
    total_qty: float
    status: str = ""
    filled: float = 0.0
    remaining: float = 0.0
    avg_fill_price: float = 0.0
    last_update: str = ""
    exec_ids: Set[str] = field(default_factory=set)
    client_id: Optional[int] = None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL

    @property
    def is_live(self) -> bool:
        return self.status in LIVE


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ExecutionRegistry:
    """Subscribes to broker events and maintains order/execution state."""

    def __init__(self, own_client_id: Optional[int] = None):
        self.own_client_id = own_client_id
        self._lock = threading.RLock()
        self._executions: Dict[str, Execution] = {}
        self._orders: Dict[int, OrderRecord] = {}
        self._by_order_id: Dict[int, int] = {}
        self._duplicates_seen = 0
        self._foreign_fills = 0

    def on_exec_details(self, trade, fill) -> Optional[Execution]:
        """execDetails: a fill happened. Returns None if already seen."""
        try:
            ex = fill.execution
            exec_id = getattr(ex, "execId", "")
            if not exec_id:
                logger.warning("execution without execId, ignoring")
                return None

            with self._lock:
                if exec_id in self._executions:
                    self._duplicates_seen += 1
                    logger.debug("duplicate execution %s ignored", exec_id)
                    return None

                contract = getattr(fill, "contract", None)
                symbol = getattr(contract, "symbol", "") if contract else ""
                client_id = getattr(ex, "clientId", None)

                execution = Execution(
                    exec_id=exec_id,
                    perm_id=getattr(ex, "permId", None),
                    order_id=getattr(ex, "orderId", None),
                    symbol=symbol,
                    side=getattr(ex, "side", ""),
                    shares=float(getattr(ex, "shares", 0) or 0),
                    price=float(getattr(ex, "price", 0) or 0),
                    time=str(getattr(ex, "time", "")),
                    account=getattr(ex, "acctNumber", ""),
                    client_id=client_id,
                )
                self._executions[exec_id] = execution

                if (self.own_client_id is not None
                        and client_id is not None
                        and client_id != self.own_client_id):
                    self._foreign_fills += 1
                    logger.warning(
                        "fill on %s from client %s (not us) - %s %s @ %s",
                        symbol, client_id, execution.side,
                        execution.shares, execution.price,
                    )

                if execution.perm_id is not None:
                    rec = self._orders.get(execution.perm_id)
                    if rec is not None:
                        rec.exec_ids.add(exec_id)

                logger.info(
                    "EXEC %s %s %s @ %.4f (execId=%s permId=%s)",
                    symbol, execution.side, execution.shares,
                    execution.price, exec_id[:16], execution.perm_id,
                )
                return execution
        except Exception as e:
            logger.error("failed to record execution: %s", e)
            return None

    def on_commission(self, trade, fill, report) -> None:
        """commissionReport: arrives separately from the fill it belongs to."""
        try:
            exec_id = getattr(report, "execId", "")
            with self._lock:
                execution = self._executions.get(exec_id)
                if execution is None:
                    return
                execution.commission = float(getattr(report, "commission", 0) or 0)
                pnl = getattr(report, "realizedPNL", None)
                if pnl is not None and pnl == pnl:
                    execution.realized_pnl = float(pnl)
        except Exception as e:
            logger.error("failed to record commission: %s", e)

    def on_order_status(self, trade) -> None:
        """orderStatus: order state changed. permId is the ownership key."""
        try:
            order = getattr(trade, "order", None)
            status_obj = getattr(trade, "orderStatus", None)
            contract = getattr(trade, "contract", None)
            if order is None or status_obj is None:
                return

            perm_id = getattr(order, "permId", None) or getattr(status_obj, "permId", None)
            order_id = getattr(order, "orderId", None)

            client_id = getattr(order, "clientId", None)

            with self._lock:
                key = perm_id if perm_id else order_id
                if key is None:
                    return

                rec = self._orders.get(key)

                # The first status of a new order (PendingSubmit) can arrive
                # before IBKR has assigned a permId. That record was keyed by
                # orderId; now that the permId is known, move it under the
                # permId instead of leaving a stale copy that looks live.
                # orderId is unique only within one client, so merge only a
                # record from the same client.
                if perm_id and order_id is not None:
                    early = self._orders.get(order_id)
                    if (early is not None and early is not rec
                            and not early.perm_id
                            and early.client_id == client_id):
                        del self._orders[order_id]
                        if rec is None:
                            early.perm_id = perm_id
                            rec = early
                            self._orders[perm_id] = rec

                if rec is None:
                    rec = OrderRecord(
                        perm_id=perm_id,
                        order_id=order_id,
                        symbol=getattr(contract, "symbol", "") if contract else "",
                        action=getattr(order, "action", ""),
                        total_qty=float(getattr(order, "totalQuantity", 0) or 0),
                        client_id=client_id,
                    )
                    # A fill can be reported before the first order status.
                    if perm_id:
                        rec.exec_ids = {e.exec_id for e in self._executions.values()
                                        if e.perm_id == perm_id}
                    self._orders[key] = rec

                rec.status = getattr(status_obj, "status", "") or rec.status
                rec.filled = float(getattr(status_obj, "filled", 0) or 0)
                rec.remaining = float(getattr(status_obj, "remaining", 0) or 0)
                avg = getattr(status_obj, "avgFillPrice", 0)
                if avg and avg == avg:
                    rec.avg_fill_price = float(avg)
                rec.last_update = _now()
                if perm_id and order_id:
                    self._by_order_id[order_id] = perm_id
        except Exception as e:
            logger.error("failed to record order status: %s", e)

    def get_order(self, perm_id=None, order_id=None) -> Optional[OrderRecord]:
        """Look up an order by permId, or by orderId if that is all we have."""
        with self._lock:
            if perm_id is not None and perm_id in self._orders:
                return self._orders[perm_id]
            if order_id is not None:
                mapped = self._by_order_id.get(order_id)
                if mapped is not None:
                    return self._orders.get(mapped)
                return self._orders.get(order_id)
            return None

    def executions_for(self, perm_id: int) -> List[Execution]:
        with self._lock:
            return [e for e in self._executions.values() if e.perm_id == perm_id]

    def filled_quantity(self, perm_id: int) -> float:
        """Cumulative filled quantity. Broker figure wins over summing."""
        with self._lock:
            rec = self._orders.get(perm_id)
            if rec is not None and rec.filled:
                return rec.filled
            return sum(e.shares for e in self._executions.values()
                       if e.perm_id == perm_id)

    def live_orders(self) -> List[OrderRecord]:
        with self._lock:
            return [r for r in self._orders.values() if r.is_live]

    def foreign_executions(self) -> List[Execution]:
        """Fills from another client or manual trading on the same account."""
        with self._lock:
            if self.own_client_id is None:
                return []
            return [e for e in self._executions.values()
                    if e.client_id is not None and e.client_id != self.own_client_id]

    def stats(self) -> dict:
        with self._lock:
            return {
                "executions": len(self._executions),
                "orders": len(self._orders),
                "duplicates_ignored": self._duplicates_seen,
                "foreign_fills": self._foreign_fills,
                "live_orders": len(self.live_orders()),
            }
