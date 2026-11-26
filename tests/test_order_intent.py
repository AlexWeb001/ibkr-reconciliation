"""Order intent log. No broker connection required."""
import json
import os
import subprocess
import sys

from conftest import SRC
from ibkr_reconciliation import (
    IntentLog, PENDING, SENT, RESOLVED, ORPHANED,
)


def test_intent_is_on_disk_immediately(tmp_path):
    path = tmp_path / "intents.jsonl"
    intent = IntentLog(path).record("AAPL", "BUY", 10, reason="signal")
    assert path.exists()
    assert intent.state == PENDING


def test_unresolved_intent_found_after_restart(tmp_path):
    path = tmp_path / "intents.jsonl"
    IntentLog(path).record("AAPL", "BUY", 10)
    assert len(IntentLog(path).find_unresolved()) == 1


def test_resolved_intent_is_closed_and_history_kept(tmp_path):
    path = tmp_path / "intents.jsonl"
    log = IntentLog(path)
    i = log.record("AAPL", "BUY", 10)
    log.mark_sent(i, order_id=42, perm_id=99887766)
    log.resolve(i, "Filled", filled_qty=10, fill_price=185.5)

    assert IntentLog(path).find_unresolved() == []
    lines = path.read_text().strip().split("\n")
    assert len(lines) == 3                      # PENDING, SENT, RESOLVED
    last = json.loads(lines[-1])
    assert last["state"] == RESOLVED
    assert last["perm_id"] == 99887766


def test_sent_without_outcome_stays_unresolved(tmp_path):
    """The dangerous case: the order left, nobody saw what happened."""
    path = tmp_path / "intents.jsonl"
    log = IntentLog(path)
    i = log.record("MSFT", "SELL", 5, reason="EOD")
    log.mark_sent(i, order_id=7)

    un = IntentLog(path).find_unresolved()
    assert len(un) == 1
    assert un[0].state == SENT
    assert un[0].order_id == 7


def test_torn_last_line_is_skipped(tmp_path):
    path = tmp_path / "intents.jsonl"
    IntentLog(path).record("MSFT", "SELL", 5)
    with path.open("a") as f:
        f.write('{"intent_id": "broken", "symbol"')     # cut mid-write
    assert len(IntentLog(path).find_unresolved()) == 1


def test_write_after_torn_line_is_not_lost(tmp_path):
    path = tmp_path / "intents.jsonl"
    with path.open("w") as f:
        f.write('{"intent_id": "broken", "symbol"')
    IntentLog(path).record("NVDA", "BUY", 1)
    un = IntentLog(path).find_unresolved()
    assert [i.symbol for i in un] == ["NVDA"]


def test_orphaned_and_abandoned_survive_reload(tmp_path):
    path = tmp_path / "intents.jsonl"
    log = IntentLog(path)
    i = log.record("MSFT", "SELL", 5)
    log.mark_sent(i, order_id=7)

    startup = IntentLog(path)
    orphan = startup.find_unresolved()[0]
    startup.mark_orphaned(orphan)
    assert any(x.state == ORPHANED for x in IntentLog(path).find_unresolved())

    startup.abandon(orphan, note="verified: never filled")
    assert IntentLog(path).find_unresolved() == []


def test_intent_survives_hard_kill(tmp_path):
    """Real process death between recording and sending (os._exit, no cleanup)."""
    path = tmp_path / "kill.jsonl"
    code = (
        "import os\n"
        "from ibkr_reconciliation import IntentLog\n"
        f"IntentLog({str(path)!r}).record('TSLA', 'BUY', 10, reason='signal')\n"
        "os._exit(9)\n"
    )
    env = dict(os.environ, PYTHONPATH=str(SRC))
    subprocess.run([sys.executable, "-c", code], env=env, check=False)

    un = IntentLog(path).find_unresolved()
    assert len(un) == 1
    assert un[0].symbol == "TSLA"
    assert un[0].state == PENDING


def test_stats_by_state(tmp_path):
    path = tmp_path / "intents.jsonl"
    log = IntentLog(path)
    i = log.record("AAPL", "BUY", 10)
    log.resolve(i, "Filled", filled_qty=10)
    log.record("AAPL", "SELL", 10)
    assert IntentLog(path).stats() == {RESOLVED: 1, PENDING: 1}
