"""
Position reconciliation between local strategy state and the broker.

The broker is the source of truth for *what* is held. The strategy is the source
of truth for *why* it is held (entry price, exit levels, signal context).

This module never places or cancels orders. It corrects local state where that is
safe and unambiguous, and reports everything else for a human to decide.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

MATCHED = "MATCHED"
QTY_MISMATCH = "QTY_MISMATCH"
BROKER_ONLY = "BROKER_ONLY"
LOCAL_ONLY = "LOCAL_ONLY"
SIDE_MISMATCH = "SIDE_MISMATCH"


@dataclass
class Discrepancy:
    symbol: str
    kind: str
    local_qty: int = 0
    local_side: str = ""
    broker_qty: int = 0
    broker_side: str = ""
    action_taken: str = ""
    needs_human: bool = False

    def describe(self) -> str:
        if self.kind == QTY_MISMATCH:
            return (f"{self.symbol}: quantity local={self.local_qty} "
                    f"broker={self.broker_qty}")
        if self.kind == SIDE_MISMATCH:
            return (f"{self.symbol}: side local={self.local_side} "
                    f"broker={self.broker_side}")
        if self.kind == BROKER_ONLY:
            return (f"{self.symbol}: held at broker ({self.broker_side} "
                    f"{self.broker_qty}) but unknown to the bot")
        if self.kind == LOCAL_ONLY:
            return (f"{self.symbol}: tracked locally ({self.local_side} "
                    f"{self.local_qty}) but not held at broker")
        return f"{self.symbol}: {self.kind}"


@dataclass
class ReconcileReport:
    checked_at: str = ""
    matched: List[str] = field(default_factory=list)
    discrepancies: List[Discrepancy] = field(default_factory=list)
    broker_unavailable: bool = False
    error: str = ""

    @property
    def clean(self) -> bool:
        return (not self.broker_unavailable and not self.error
                and not self.discrepancies)

    @property
    def blocking(self) -> List[Discrepancy]:
        return [d for d in self.discrepancies if d.needs_human]

    def summary(self) -> str:
        if self.error:
            return f"Reconciliation failed: {self.error}"
        if self.clean:
            return f"Reconciliation clean: {len(self.matched)} position(s) match"
        lines = [f"Reconciliation found {len(self.discrepancies)} discrepancy(ies):"]
        lines += [f"  - {d.describe()}" + (f" [{d.action_taken}]" if d.action_taken else "")
                  for d in self.discrepancies]
        if self.blocking:
            lines.append(f"{len(self.blocking)} require manual review.")
        return "\n".join(lines)


def _normalise_broker_positions(raw, symbols: Optional[set]) -> Dict[str, dict]:
    """
    Convert ib_async / ib_insync Position objects into {symbol: {side, qty}}.

    A negative broker quantity means short. Symbols outside `symbols` are ignored
    so other bots or manual trades on the same account are not treated as ours.
    """
    out: Dict[str, dict] = {}
    for p in raw or []:
        try:
            sym = p.contract.symbol
            qty = int(p.position)
        except Exception:
            logger.warning("Skipping unreadable broker position: %r", p)
            continue
        if qty == 0:
            continue
        if symbols is not None and sym not in symbols:
            continue
        out[sym] = {"side": "LONG" if qty > 0 else "SHORT", "qty": abs(qty)}
    return out


def reconcile(strategy, connector, extra_symbols=None,
              apply_fixes: bool = True) -> ReconcileReport:
    """
    Compare local positions with the broker and correct what is safe to correct.

    Corrected automatically:
      - quantity mismatch  -> local quantity follows the broker
      - local-only ghost   -> dropped locally (nothing is held)

    Reported for a human, never auto-corrected:
      - broker-only position (no entry price or exit levels are known for it)
      - side mismatch (a genuine contradiction, likely manual intervention)
    """
    report = ReconcileReport(checked_at=datetime.now().isoformat())

    if connector is None or not getattr(connector, "connected", False):
        report.broker_unavailable = True
        report.error = "not connected to broker"
        logger.error("Reconciliation skipped: not connected")
        return report

    try:
        raw = connector.get_positions()
    except Exception as e:
        report.error = str(e)
        logger.error("Reconciliation failed to read broker positions: %s", e)
        return report

    local = strategy.get_open_positions()
    watched = set(local) | set(extra_symbols or [])
    broker = _normalise_broker_positions(raw, watched or None)

    for sym in sorted(set(local) | set(broker)):
        lp = local.get(sym)
        bp = broker.get(sym)

        if lp and bp:
            if lp["side"] != bp["side"]:
                report.discrepancies.append(Discrepancy(
                    symbol=sym, kind=SIDE_MISMATCH,
                    local_qty=lp["qty"], local_side=lp["side"],
                    broker_qty=bp["qty"], broker_side=bp["side"],
                    needs_human=True,
                    action_taken="none - manual review required",
                ))
            elif lp["qty"] != bp["qty"]:
                d = Discrepancy(
                    symbol=sym, kind=QTY_MISMATCH,
                    local_qty=lp["qty"], local_side=lp["side"],
                    broker_qty=bp["qty"], broker_side=bp["side"],
                )
                if apply_fixes:
                    strategy.set_position_quantity(sym, bp["qty"])
                    d.action_taken = f"local quantity set to {bp['qty']}"
                report.discrepancies.append(d)
            else:
                report.matched.append(sym)

        elif lp and not bp:
            d = Discrepancy(
                symbol=sym, kind=LOCAL_ONLY,
                local_qty=lp["qty"], local_side=lp["side"],
            )
            if apply_fixes:
                strategy.drop_position(sym, reason="not held at broker")
                d.action_taken = "dropped locally"
            report.discrepancies.append(d)

        else:
            report.discrepancies.append(Discrepancy(
                symbol=sym, kind=BROKER_ONLY,
                broker_qty=bp["qty"], broker_side=bp["side"],
                needs_human=True,
                action_taken="none - bot has no entry context for this position",
            ))

    logger.info(report.summary())
    return report
