"""
Order and position state safety for Interactive Brokers (TWS / IB Gateway).

Three small, independent pieces:

  order_intent        - write the decision to disk before the order leaves
  execution_registry  - record fills from events, deduplicated by execId
  reconciliation      - compare local positions with the broker

None of them imports a broker library. They read the attributes that
ib_async / ib_insync objects carry, so they can be tested offline.
"""

from .order_intent import (
    IntentLog, OrderIntent,
    PENDING, SENT, RESOLVED, ORPHANED, ABANDONED,
)
from .execution_registry import ExecutionRegistry, Execution, OrderRecord
from .reconciliation import (
    reconcile, ReconcileReport, Discrepancy,
    MATCHED, QTY_MISMATCH, BROKER_ONLY, LOCAL_ONLY, SIDE_MISMATCH,
)

__version__ = "0.1.0"

__all__ = [
    "IntentLog", "OrderIntent",
    "PENDING", "SENT", "RESOLVED", "ORPHANED", "ABANDONED",
    "ExecutionRegistry", "Execution", "OrderRecord",
    "reconcile", "ReconcileReport", "Discrepancy",
    "MATCHED", "QTY_MISMATCH", "BROKER_ONLY", "LOCAL_ONLY", "SIDE_MISMATCH",
]
