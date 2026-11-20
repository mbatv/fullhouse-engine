"""
Fullhouse Bot Runner — executes inside the Docker sandbox (or directly under
Python locally for dev). Loads /bot/bot.py, reads game states from stdin,
writes actions to stdout. All communication is newline-delimited JSON.

Cross-platform note
-------------------
We used to enforce the 2 s/decision timeout with signal.SIGALRM, which is
Unix-only. On Windows that crashed at module load. Now we run decide() on
a worker thread and wait on an Event with timeout. Same per-call deadline
behaviour, works identically on Linux / macOS / Windows. The only caveat
is that Python can't actually kill a thread mid-execution — so a bot stuck
in a tight C-extension loop will continue computing in the background
until it finishes or the container is OOM-killed. We handle that case by
emitting fold immediately on timeout and moving on; the stuck thread is
daemon-flagged so it dies with the process. While that stale thread is
still running, further requests are answered with fold immediately rather
than starting a second decide() alongside it.

Protocol isolation
------------------
The protocol channel is a private duplicate of the original stdout. File
descriptor 1 (and sys.stdout) are pointed at stderr before the bot is
loaded, so a bot's print() calls -- or output from C extensions -- can never
corrupt or desynchronise the JSON stream.
"""

import sys
import json
import importlib.util
import traceback
import threading
import os

BOT_PATH        = os.environ.get("BOT_PATH", "/bot/bot.py")
TIMEOUT         = int(os.environ.get("ACTION_TIMEOUT", "2"))
WARMUP_TIMEOUT  = int(os.environ.get("WARMUP_TIMEOUT", "30"))


def load_bot(path: str):
    spec   = importlib.util.spec_from_file_location("bot", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "decide"):
        raise AttributeError("bot.py must define a decide() function")
    return module


_PROTOCOL_OUT = None


def _isolate_stdout():
    """Move the protocol onto a private fd and send fd 1 to stderr."""
    global _PROTOCOL_OUT
    _PROTOCOL_OUT = os.fdopen(os.dup(1), "w", buffering=1)
    sys.stdout.flush()
    os.dup2(2, 1)
    sys.stdout = sys.stderr


def emit(obj: dict):
    out = _PROTOCOL_OUT or sys.stdout
    out.write(json.dumps(obj) + "\n")
    out.flush()


class _BotTimeout(Exception):
    pass


class _BotBusy(Exception):
    pass


_stale_worker = None


def _call_with_timeout(fn, arg, timeout_s):
    """Run fn(arg) on a worker thread and wait up to timeout_s for it to
    finish. Returns the result, or raises _BotTimeout. Re-raises any
    exception fn raised. Cross-platform — no signals.

    Raises _BotBusy without calling fn if a previously timed-out call is
    still running, so a bot never has two decide() calls in flight."""
    global _stale_worker
    if _stale_worker is not None:
        if _stale_worker.is_alive():
            raise _BotBusy()
        _stale_worker = None

    box = {"value": None, "error": None}
    done = threading.Event()

    def _worker():
        try:
            box["value"] = fn(arg)
        except BaseException as e:   # bots can raise anything
            box["error"] = e
        finally:
            done.set()

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    if not done.wait(timeout_s):
        # Thread may keep running (Python can't preempt) but we move on.
        # daemon=True ensures it dies with the process.
        _stale_worker = t
        raise _BotTimeout()
    if box["error"] is not None:
        raise box["error"]
    return box["value"]


def main():
    _isolate_stdout()
    try:
        bot = load_bot(BOT_PATH)
    except Exception as e:
        sys.stderr.write(f"[runner] LOAD ERROR: {e}\n")
        # Drain stdin so the parent doesn't deadlock on a write.
        for _ in sys.stdin:
            emit({"action": "fold", "error": "load_failed"})
        return

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            state = json.loads(line)
        except json.JSONDecodeError as e:
            sys.stderr.write(f"[runner] BAD JSON: {e}\n")
            emit({"action": "fold", "error": "bad_json"})
            continue

        # Warm-up: one-shot call before hand 1 with a longer budget so bots
        # loading CFR blueprints / NN weights don't get killed at 2 s.
        # We don't penalise warmup errors — failure here just means hand 1
        # will run with whatever's loaded.
        is_warmup = state.get("type") == "warmup"
        budget = WARMUP_TIMEOUT if is_warmup else TIMEOUT

        try:
            action = _call_with_timeout(bot.decide, state, budget)
            if is_warmup:
                emit({"ok": True})
                continue
            if not isinstance(action, dict) or "action" not in action:
                raise ValueError("decide() must return dict with 'action' key")
            emit(action)
        except _BotBusy:
            sys.stderr.write("[runner] BUSY (previous decide() still running)\n")
            emit({"action": "fold", "error": "busy"} if not is_warmup
                 else {"ok": False, "error": "busy"})
        except _BotTimeout:
            if is_warmup:
                sys.stderr.write("[runner] WARMUP TIMEOUT\n")
                emit({"ok": False, "error": "warmup_timeout"})
                continue
            sys.stderr.write("[runner] TIMEOUT\n")
            emit({"action": "fold", "error": "timeout"})
        except Exception:
            if is_warmup:
                sys.stderr.write(f"[runner] WARMUP EXCEPTION:\n{traceback.format_exc()}\n")
                emit({"ok": False, "error": "warmup_exception"})
                continue
            sys.stderr.write(f"[runner] BOT EXCEPTION:\n{traceback.format_exc()}\n")
            emit({"action": "fold", "error": "exception"})


if __name__ == "__main__":
    main()
