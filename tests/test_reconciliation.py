"""Position reconciliation. No broker connection required."""
from types import SimpleNamespace

from ibkr_reconciliation import (
    reconcile, QTY_MISMATCH, BROKER_ONLY, LOCAL_ONLY, SIDE_MISMATCH,
)


def pos(symbol, qty):
    """Shaped like an ib_async Position: .contract.symbol, .position."""
    return SimpleNamespace(contract=SimpleNamespace(symbol=symbol), position=qty)


class FakeStrategy:
    def __init__(self, positions):
        self.p = {k: dict(v) for k, v in positions.items()}
        self.log = []

    def get_open_positions(self):
        return {k: dict(v) for k, v in self.p.items()}

    def set_position_quantity(self, symbol, quantity):
        self.p[symbol]["qty"] = quantity
        self.log.append(("qty", symbol, quantity))

    def drop_position(self, symbol, reason=""):
        self.p.pop(symbol, None)
        self.log.append(("drop", symbol, reason))


class FakeConnector:
    def __init__(self, positions, connected=True, raises=False):
        self._p, self.connected, self._raises = positions, connected, raises

    def get_positions(self):
        if self._raises:
            raise RuntimeError("TWS timeout")
        return self._p


LONG_10 = {"AA": {"side": "LONG", "qty": 10}}


def test_match_is_clean():
    r = reconcile(FakeStrategy(LONG_10), FakeConnector([pos("AA", 10)]))
    assert r.clean and r.matched == ["AA"]


def test_quantity_mismatch_follows_broker():
    s = FakeStrategy(LONG_10)
    r = reconcile(s, FakeConnector([pos("AA", 4)]))
    assert r.discrepancies[0].kind == QTY_MISMATCH
    assert s.p["AA"]["qty"] == 4


def test_local_ghost_is_dropped():
    s = FakeStrategy(LONG_10)
    r = reconcile(s, FakeConnector([]))
    assert r.discrepancies[0].kind == LOCAL_ONLY
    assert "AA" not in s.p


def test_broker_only_position_goes_to_human():
    """No entry price, no exit levels: the bot must not adopt it."""
    s = FakeStrategy({})
    r = reconcile(s, FakeConnector([pos("AA", 10)]), extra_symbols=["AA"])
    d = r.discrepancies[0]
    assert d.kind == BROKER_ONLY and d.needs_human
    assert s.log == []


def test_side_mismatch_goes_to_human():
    s = FakeStrategy(LONG_10)
    r = reconcile(s, FakeConnector([pos("AA", -10)]))
    d = r.discrepancies[0]
    assert d.kind == SIDE_MISMATCH and d.needs_human
    assert s.p["AA"]["qty"] == 10 and s.log == []


def test_unrelated_symbol_on_account_ignored():
    r = reconcile(FakeStrategy(LONG_10),
                  FakeConnector([pos("AA", 10), pos("TSLA", 500)]))
    assert r.clean


def test_short_matches_by_sign():
    s = FakeStrategy({"AA": {"side": "SHORT", "qty": 10}})
    assert reconcile(s, FakeConnector([pos("AA", -10)])).clean


def test_not_connected_changes_nothing():
    s = FakeStrategy(LONG_10)
    r = reconcile(s, FakeConnector([], connected=False))
    assert r.broker_unavailable and not r.clean
    assert s.p == LONG_10 and s.log == []


def test_broker_error_changes_nothing():
    """An empty answer after an error is not 'no positions'."""
    s = FakeStrategy(LONG_10)
    r = reconcile(s, FakeConnector([], raises=True))
    assert r.error and s.log == []


def test_report_only_mode():
    s = FakeStrategy(LONG_10)
    r = reconcile(s, FakeConnector([]), apply_fixes=False)
    assert r.discrepancies and "AA" in s.p


def test_summary_mentions_manual_review():
    s = FakeStrategy(LONG_10)
    r = reconcile(s, FakeConnector([pos("AA", -10)]))
    assert "manual review" in r.summary()


def test_flat_bot_ignores_manual_position():
    """The bot holds nothing and watches nothing: a manual trade is not its business."""
    s = FakeStrategy({})
    r = reconcile(s, FakeConnector([pos("TSLA", 500)]))
    assert r.clean and r.discrepancies == [] and s.log == []


def test_checked_at_is_utc():
    r = reconcile(FakeStrategy(LONG_10), FakeConnector([pos("AA", 10)]))
    assert r.checked_at.endswith("+00:00")
