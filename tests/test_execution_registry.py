"""Execution registry. No broker connection required."""
from types import SimpleNamespace as Obj

from ibkr_reconciliation import ExecutionRegistry


def make_fill(exec_id, symbol="AAPL", shares=10, price=185.5,
              perm_id=555001, order_id=42, client_id=7, side="BOT"):
    """Shaped like an ib_async Fill: .execution and .contract."""
    return Obj(
        execution=Obj(execId=exec_id, permId=perm_id, orderId=order_id,
                      side=side, shares=shares, price=price,
                      time="20260817 15:30:00", acctNumber="DU0000000",
                      clientId=client_id),
        contract=Obj(symbol=symbol),
    )


def make_trade(status="Filled", filled=10, remaining=0, avg=185.5,
               perm_id=555001, order_id=42, symbol="AAPL",
               action="BUY", total=10, client_id=7):
    """Shaped like an ib_async Trade: .order, .orderStatus, .contract."""
    return Obj(
        order=Obj(permId=perm_id, orderId=order_id, action=action,
                  totalQuantity=total, clientId=client_id),
        orderStatus=Obj(status=status, filled=filled, remaining=remaining,
                        avgFillPrice=avg, permId=perm_id),
        contract=Obj(symbol=symbol),
    )


def test_fill_is_recorded():
    r = ExecutionRegistry(own_client_id=7)
    e = r.on_exec_details(None, make_fill("0001.abc"))
    assert e is not None and e.shares == 10


def test_duplicate_exec_id_does_not_double_count():
    """Reconnects replay executions. A double-counted fill is an invented position."""
    r = ExecutionRegistry(own_client_id=7)
    r.on_exec_details(None, make_fill("0001.abc"))
    assert r.on_exec_details(None, make_fill("0001.abc")) is None
    assert r.filled_quantity(555001) == 10
    assert r.stats()["duplicates_ignored"] == 1


def test_partial_fills_add_up():
    r = ExecutionRegistry(own_client_id=7)
    r.on_exec_details(None, make_fill("0001.abc"))
    r.on_exec_details(None, make_fill("0002.abc", shares=5))
    assert r.filled_quantity(555001) == 15


def test_broker_cumulative_quantity_wins():
    r = ExecutionRegistry(own_client_id=7)
    r.on_exec_details(None, make_fill("0001.abc"))
    r.on_order_status(make_trade(filled=15, avg=185.6))
    assert r.filled_quantity(555001) == 15
    assert r.get_order(perm_id=555001).avg_fill_price == 185.6


def test_lookup_by_order_id():
    r = ExecutionRegistry(own_client_id=7)
    r.on_order_status(make_trade())
    assert r.get_order(order_id=42) is not None


def test_commission_attaches_to_its_fill():
    r = ExecutionRegistry(own_client_id=7)
    r.on_exec_details(None, make_fill("0001.abc"))
    r.on_commission(None, None, Obj(execId="0001.abc", commission=1.25,
                                    realizedPNL=0.0))
    [e] = r.executions_for(555001)
    assert e.commission == 1.25


def test_commission_nan_pnl_ignored():
    r = ExecutionRegistry(own_client_id=7)
    r.on_exec_details(None, make_fill("0001.abc"))
    r.on_commission(None, None, Obj(execId="0001.abc", commission=1.0,
                                    realizedPNL=float("nan")))
    [e] = r.executions_for(555001)
    assert e.realized_pnl == 0.0


def test_foreign_client_fill_is_flagged():
    r = ExecutionRegistry(own_client_id=7)
    r.on_exec_details(None, make_fill("9001.x", client_id=99, symbol="TSLA"))
    r.on_exec_details(None, make_fill("9002.x", client_id=7))
    assert len(r.foreign_executions()) == 1
    assert r.stats()["foreign_fills"] == 1


def test_live_and_terminal_orders_are_told_apart():
    r = ExecutionRegistry(own_client_id=7)
    r.on_order_status(make_trade(status="Submitted", filled=0, remaining=10,
                                 perm_id=777, order_id=1))
    r.on_order_status(make_trade(status="Filled", perm_id=778, order_id=2))
    assert [o.perm_id for o in r.live_orders()] == [777]


def test_bad_events_do_not_break_registry():
    r = ExecutionRegistry(own_client_id=7)
    assert r.on_exec_details(None, Obj(execution=Obj(execId=""),
                                       contract=Obj(symbol="X"))) is None
    assert r.on_exec_details(None, Obj()) is None
    r.on_order_status(Obj())
    assert r.stats()["executions"] == 0


def test_other_clients_order_tracked_by_perm_id():
    """orderId is per client session; permId is what identifies a manual/TWS order."""
    r = ExecutionRegistry(own_client_id=7)
    r.on_order_status(make_trade(status="Submitted", perm_id=888, order_id=500,
                                 client_id=99, symbol="NVDA"))
    rec = r.get_order(perm_id=888)
    assert rec.client_id == 99 and rec.symbol == "NVDA"


def test_status_before_perm_id_is_merged():
    """PendingSubmit can come with permId = 0; the next status carries the real one."""
    r = ExecutionRegistry(own_client_id=7)
    r.on_order_status(make_trade(status="PendingSubmit", filled=0, remaining=10,
                                 perm_id=0, order_id=42))
    assert [o.status for o in r.live_orders()] == ["PendingSubmit"]

    r.on_order_status(make_trade(status="Submitted", filled=0, remaining=10,
                                 perm_id=9001, order_id=42))
    assert r.stats()["orders"] == 1
    rec = r.get_order(perm_id=9001)
    assert rec.perm_id == 9001 and rec.status == "Submitted"
    assert r.get_order(order_id=42) is rec

    r.on_order_status(make_trade(status="Cancelled", filled=0, remaining=10,
                                 perm_id=9001, order_id=42))
    assert r.live_orders() == []


def test_fill_after_merge_attaches_to_order():
    r = ExecutionRegistry(own_client_id=7)
    r.on_order_status(make_trade(status="PendingSubmit", filled=0, remaining=10,
                                 perm_id=0, order_id=42))
    r.on_order_status(make_trade(status="Submitted", filled=0, remaining=10,
                                 perm_id=9001, order_id=42))
    r.on_exec_details(None, make_fill("0001.abc", perm_id=9001, order_id=42))
    assert r.get_order(perm_id=9001).exec_ids == {"0001.abc"}


def test_same_order_id_from_other_client_not_merged():
    """orderId is unique only within one API client."""
    r = ExecutionRegistry(own_client_id=7)
    r.on_order_status(make_trade(status="PendingSubmit", filled=0, remaining=10,
                                 perm_id=0, order_id=42, client_id=7))
    r.on_order_status(make_trade(status="Submitted", filled=0, remaining=5,
                                 perm_id=8001, order_id=42, client_id=99,
                                 symbol="NVDA"))
    assert r.stats()["orders"] == 2
    assert r.get_order(perm_id=8001).client_id == 99


def test_stale_copy_dropped_when_perm_id_record_exists():
    """Events out of order: permId record first, then a late permId-less one."""
    r = ExecutionRegistry(own_client_id=7)
    r.on_order_status(make_trade(status="Submitted", filled=0, remaining=10,
                                 perm_id=9001, order_id=42))
    r.on_order_status(make_trade(status="PendingSubmit", filled=0, remaining=10,
                                 perm_id=0, order_id=42))
    r.on_order_status(make_trade(status="Filled", perm_id=9001, order_id=42))
    assert r.stats()["orders"] == 1
    assert r.live_orders() == []


def test_pending_cancel_is_still_live():
    """Cancel requested, not confirmed: the order can still fill."""
    r = ExecutionRegistry(own_client_id=7)
    r.on_order_status(make_trade(status="PendingCancel", filled=0, remaining=10,
                                 perm_id=777, order_id=1))
    assert [o.perm_id for o in r.live_orders()] == [777]
    r.on_order_status(make_trade(status="Cancelled", filled=0, remaining=10,
                                 perm_id=777, order_id=1))
    assert r.live_orders() == []


def test_api_pending_is_live():
    r = ExecutionRegistry(own_client_id=7)
    r.on_order_status(make_trade(status="ApiPending", filled=0, remaining=10,
                                 perm_id=777, order_id=1))
    assert len(r.live_orders()) == 1


def test_fill_before_first_status_attaches_to_order():
    r = ExecutionRegistry(own_client_id=7)
    r.on_exec_details(None, make_fill("0001.abc", perm_id=555001))
    r.on_order_status(make_trade(perm_id=555001))
    assert r.get_order(perm_id=555001).exec_ids == {"0001.abc"}
