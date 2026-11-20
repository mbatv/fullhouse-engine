"""Tests for the bot process wrapper and runner (local subprocess mode)."""
import sys, os, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

import sandbox.match as match
from sandbox.match import BotProcess, run_match

STATE = {"type": "action_request", "hand_id": "t", "street": "preflop",
         "amount_owed": 100, "can_check": False, "your_cards": ["As", "Kh"]}


def write_bot(tmp_path, name, body):
    d = tmp_path / name
    d.mkdir()
    (d / "bot.py").write_text(body)
    return str(d)


@pytest.fixture
def short_timeout(monkeypatch):
    monkeypatch.setattr(match, "ACTION_TIMEOUT", 1)
    monkeypatch.setattr(match, "HOST_GRACE_S", 1.0)


def test_bot_print_does_not_corrupt_protocol(tmp_path):
    path = write_bot(tmp_path, "printer",
                     "def decide(s):\n    print('debug', s.get('street'))\n"
                     "    return {'action': 'call'}\n")
    bot = BotProcess("printer", path)
    try:
        for _ in range(3):
            assert bot.act(STATE) == {"action": "call"}
        assert bot.errors == []
    finally:
        bot.stop()


def test_timed_out_decision_is_not_overlapped(tmp_path, short_timeout):
    path = write_bot(tmp_path, "sleeper",
                     "import time\n"
                     "def decide(s):\n    time.sleep(2.5)\n    return {'action': 'call'}\n")
    bot = BotProcess("sleeper", path)
    try:
        assert bot.act(STATE)["error"] == "timeout"
        # The first decide() is still sleeping: the runner must not start another.
        assert bot.act(STATE)["error"] == "busy"
        time.sleep(2.0)   # let the stale call finish
        assert bot.act(STATE)["error"] == "timeout"
        assert not bot.dead
    finally:
        bot.stop()


def test_wedged_bot_is_killed_by_host_timeout(tmp_path, short_timeout):
    # Catastrophic regex backtracking holds the GIL, so the runner's own
    # timer can never fire. The host deadline has to catch it.
    path = write_bot(tmp_path, "wedged",
                     "import re\n"
                     "def decide(s):\n"
                     "    re.match(r'(a+)+$', 'a' * 40 + 'b')\n"
                     "    return {'action': 'call'}\n")
    bot = BotProcess("wedged", path)
    try:
        t0 = time.monotonic()
        assert bot.act(STATE)["error"] == "host_timeout"
        assert time.monotonic() - t0 < 4
        assert bot.dead
        assert bot.act(STATE) == {"action": "fold", "error": "no_process"}
    finally:
        bot.stop()


def test_match_with_custom_stack_scores_against_that_stack():
    bots = {"shark": "bots/shark/bot.py", "aggressor": "bots/aggressor/bot.py",
            "template": "bots/template/bot.py"}
    here = os.path.join(os.path.dirname(__file__), "..")
    bots = {k: os.path.join(here, v) for k, v in bots.items()}
    r = run_match("t", bots, n_hands=30, seed=11, starting_stack=2500)
    assert r["starting_stack"] == 2500
    assert sum(r["chip_delta"].values()) == 0
    assert all(r["final_stacks"][b] - 2500 == r["chip_delta"][b] for b in bots)
    assert all(not e for e in r["bot_errors"].values())


def test_match_log_records_applied_action(tmp_path):
    # "check" while facing a bet is applied as a call; the log must say so.
    path = write_bot(tmp_path, "checker",
                     "def decide(s):\n    return {'action': 'check'}\n")
    r = run_match("t", {"c1": path, "c2": path}, n_hands=1, seed=1)
    first = r["hands"][0]["action_log"]
    assert any(a["action"] == "call" for a in first)
