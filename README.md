# ibkr-reconciliation

[![tests](https://github.com/AlexWeb001/ibkr-reconciliation/actions/workflows/tests.yml/badge.svg)](https://github.com/AlexWeb001/ibkr-reconciliation/actions/workflows/tests.yml)

Order and position state safety for automated trading on **Interactive Brokers** (TWS / IB Gateway).

Three small modules I wrote for my own IBKR trading systems and reuse in client work. They do not place orders and do not decide anything about the market. They answer one question: **does the program know what is really happening on the account?**

| Module | Problem it solves |
| --- | --- |
| `order_intent` | The process dies after deciding to send an order. On restart: was it sent or not? |
| `execution_registry` | Fills arrive late, twice, or from another client. How much is really filled? |
| `reconciliation` | The bot thinks it holds 10, the broker says 4 — or the other way round. Who wins? |

No broker library is imported. The modules read the attributes that `ib_async` / `ib_insync` objects carry, so the whole suite runs offline in a fraction of a second.

## Why this exists: things IBKR actually does

These are behaviours I ran into on real paper accounts, not theory.

**The API echo is not a confirmation.** `placeOrder()` returns a `Trade` immediately. That only means the request left your process. An order can still be rejected, held, or never reach the exchange.

**"Cancelled" is not always cancelled.** Error 10349 ("Order TIF was set to DAY based on order preset") comes with a status that looks like a cancel, while the order may still be working. Resending on that signal can double the position. Rule: set TIF explicitly, and never resend until the broker's state has been re-read.

**Position updates can arrive before the fill.** TWS may report the new position before the execution that caused it. Code that reads "position changed → look at last fill" sees the old fill. The position must be read from the broker after the fill has settled, not inferred.

**Executions are replayed.** After a reconnect, or when requesting execution history, the same `execId` comes again. Summing quantities without deduplication invents a position.

**`orderId` is per client session; `permId` is not.** Orders entered manually in TWS, or by another API client, have someone else's `orderId`. `permId` is assigned by IBKR and survives reconnects and restarts.

**A timeout is not a refusal.** A fill that arrives 30 seconds after your code stopped waiting is still a fill.

**An empty answer after an error is not "no positions".** If reading positions failed, flattening local state to match would wipe out everything the bot knows.

## The modules

### `order_intent` — write it down before you send it

```python
from ibkr_reconciliation import IntentLog

log = IntentLog("data/intents.jsonl")

intent = log.record("MES", "SELL", 1, reason="stop breached")   # fsync'd to disk
trade = ib.placeOrder(contract, order)
log.mark_sent(intent, order_id=trade.order.orderId)
# ... later, when the broker gives a terminal status:
log.resolve(intent, "Filled", filled_qty=1, fill_price=5871.25,
            perm_id=trade.order.permId)
```

On startup:

```python
for intent in log.find_unresolved():
    log.mark_orphaned(intent)
    # ask the broker what happened to it (open orders, executions by permId)
    # then: log.resolve(...) or log.abandon(intent, note="verified: never filled")
```

- Append-only JSONL, `fsync` on every record. A hard kill can tear at most the last line; the history stays readable and the next write is not glued onto the fragment.
- `PENDING` (decided, maybe not sent) and `SENT` (sent, outcome unknown) are kept apart because they need opposite recovery actions.
- Tested with a real `os._exit()` between recording and sending.

### `execution_registry` — fills from events, deduplicated

```python
from ibkr_reconciliation import ExecutionRegistry

reg = ExecutionRegistry(own_client_id=1)
ib.execDetailsEvent += reg.on_exec_details
ib.commissionReportEvent += reg.on_commission
ib.orderStatusEvent += reg.on_order_status

reg.filled_quantity(perm_id)    # broker's cumulative figure wins over summing
reg.foreign_executions()        # fills from manual trading or another client
reg.live_orders()
```

- Deduplicates on `execId`.
- Orders are keyed by `permId`, so manual TWS orders and other clients are visible.
- Commission reports, which arrive separately, are attached to their fill. NaN `realizedPNL` is ignored.
- A malformed event is logged and dropped; it never takes the event loop down.

### `reconciliation` — the broker owns *what*, the bot owns *why*

```python
from ibkr_reconciliation import reconcile

report = reconcile(strategy, connector, extra_symbols=["MES"])
print(report.summary())
if report.blocking:
    stop_trading_and_alert(report)
```

| Situation | Action |
| --- | --- |
| Same side, same quantity | matched |
| Same side, different quantity | local quantity follows the broker |
| Tracked locally, not held at broker | dropped locally |
| Held at broker, unknown to the bot | **reported, needs a human** — no entry price or exit levels are known |
| Opposite side | **reported, needs a human** — a real contradiction, usually manual intervention |
| Not connected / broker error | **nothing changes** |

Never places or cancels orders. Symbols the bot does not trade are ignored, so manual positions on the same account are left alone. `apply_fixes=False` gives a report-only mode.

`strategy` needs `get_open_positions()`, `set_position_quantity()`, `drop_position()`; `connector` needs `connected` and `get_positions()`. See the fakes in `tests/` for the exact shapes.

## Known limitations

- **Order status before `permId`.** If the first `orderStatus` for an order arrives with `permId = 0` (it can, in `PendingSubmit`), the registry keys that record by `orderId`. Later events with the real `permId` create a second record, and the first one can stay "live". Covered by an `xfail` test; to be fixed by merging on `orderId`.
- `reconciliation` compares whole positions per symbol. Two strategies trading the same symbol on one account need their own attribution layer.
- Quantities in `reconciliation` are integers (stocks, futures). Fractional shares are not handled.

## Running the tests

```bash
python -m pip install pytest
python -m pytest -q
```

Python 3.10+, no runtime dependencies.

## Context

I build Python trading systems on the IBKR TWS API: an ADX trend system for US equities with a scanner and a universe manager (paper trading, 1,700+ trades), a TradingView-webhook receiver, and position protection for futures traders. These modules are the part of that work that every system needs and that I can publish.

## License

MIT — see [LICENSE](LICENSE).
