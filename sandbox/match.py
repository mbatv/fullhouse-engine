"""
Fullhouse Match Orchestrator
Runs a multi-hand match between N bots (2-9).

Dev mode (USE_DOCKER=false):  bots run as local subprocesses via runner.py
Prod mode (USE_DOCKER=true):  bots run in isolated Docker containers

Submission formats supported (auto-detected from path):
  - bot.py         single-file bot (legacy)
  - bot/           directory containing bot.py + optional data/
  - bot.zip        archive containing bot.py at root + optional data/

The game engine is pure Python — this file handles all I/O and process management.
"""

import json
import os
import selectors
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from engine.game import (PokerEngine, MAX_PLAYERS, SMALL_BLIND, BIG_BLIND,
                         STARTING_STACK)

RUNNER_PATH    = Path(__file__).parent / "runner.py"
SANDBOX_IMAGE  = os.environ.get("SANDBOX_IMAGE", "fullhouse-sandbox:latest")
USE_DOCKER     = os.environ.get("USE_DOCKER", "false").lower() == "true"
ACTION_TIMEOUT = int(os.environ.get("ACTION_TIMEOUT", "2"))
WARMUP_TIMEOUT = int(os.environ.get("WARMUP_TIMEOUT", "30"))

# Host-side deadline on top of the runner's own timer. The runner normally
# answers with a fold at ACTION_TIMEOUT; if it does not answer within the
# grace period it is wedged (e.g. a C extension holding the GIL) and is killed.
HOST_GRACE_S         = float(os.environ.get("HOST_GRACE_S", "1.0"))
CONTAINER_START_GRACE_S = float(os.environ.get("CONTAINER_START_GRACE_S", "15"))

# Pin numeric-library thread pools to one thread so bots can't gain (or lose)
# from oversubscribing their CPU quota.
THREAD_CAP_ENV = {
    "OMP_NUM_THREADS":        "1",
    "OPENBLAS_NUM_THREADS":   "1",
    "MKL_NUM_THREADS":        "1",
    "NUMEXPR_NUM_THREADS":    "1",
    "VECLIB_MAXIMUM_THREADS": "1",
}

# Resource limits enforced at the container level. Bumped from 256 -> 768 MB
# in May 2026 to accommodate optional /bot/data/ payloads (CFR blueprints,
# NN weights, lookup tables) that bots load at module-import time.
CONTAINER_MEMORY     = os.environ.get("BOT_MEMORY", "768m")
CONTAINER_CPUS       = os.environ.get("BOT_CPUS",   "0.5")
CONTAINER_TMPFS_SIZE = os.environ.get("BOT_TMPFS",  "20m")
CONTAINER_PIDS_LIMIT = os.environ.get("BOT_PIDS",   "64")

# Per-match rolling action log exposed to bots in state["match_action_log"].
# Lets bots build cross-hand opponent models within a match.
MATCH_LOG_MAX_ENTRIES = 200


# ---------------------------------------------------------------------------
# Bot mount preparation
# ---------------------------------------------------------------------------

def _prepare_bot_mount(bot_path):
    """Returns (mount_src, cleanup_dir).
    Accepts: directory, .zip archive (extracted into tempdir), or .py file (legacy, copied into tempdir).
    """
    p = os.path.abspath(bot_path)

    if os.path.isdir(p):
        return p, None

    if p.endswith(".zip") and os.path.isfile(p):
        tmpdir = tempfile.mkdtemp(prefix="fhbot_")
        with zipfile.ZipFile(p) as zf:
            for member in zf.infolist():
                name = member.filename
                if name.startswith("/") or name.startswith("\\"):
                    shutil.rmtree(tmpdir, ignore_errors=True)
                    raise ValueError("Unsafe zip path (absolute): " + repr(name))
                norm = os.path.normpath(os.path.join(tmpdir, name))
                if not norm.startswith(tmpdir + os.sep) and norm != tmpdir:
                    shutil.rmtree(tmpdir, ignore_errors=True)
                    raise ValueError("Unsafe zip path (traversal): " + repr(name))
                if (member.external_attr >> 16) & 0o170000 == 0o120000:
                    shutil.rmtree(tmpdir, ignore_errors=True)
                    raise ValueError("Unsafe zip path (symlink): " + repr(name))
            zf.extractall(tmpdir)
        if not os.path.isfile(os.path.join(tmpdir, "bot.py")):
            shutil.rmtree(tmpdir, ignore_errors=True)
            raise ValueError("Zip archive must contain bot.py at the root")
        return tmpdir, tmpdir

    if p.endswith(".py") and os.path.isfile(p):
        tmpdir = tempfile.mkdtemp(prefix="fhbot_")
        shutil.copy(p, os.path.join(tmpdir, "bot.py"))
        return tmpdir, tmpdir

    raise ValueError("Unsupported bot path (must be .py, .zip, or directory): " + repr(p))


# ---------------------------------------------------------------------------
# Bot process wrapper
# ---------------------------------------------------------------------------

class BotProcess:
    """Wraps one bot in a subprocess or Docker container.
    Communication: newline-delimited JSON over stdin/stdout.
    """

    def __init__(self, bot_id, bot_path):
        self.bot_id   = bot_id
        self.bot_path = bot_path
        self.errors   = []
        self.dead     = False
        self._container = None
        self._cleanup_dir = None
        self._buf     = b""

        try:
            self._mount_src, self._cleanup_dir = _prepare_bot_mount(bot_path)
        except Exception as e:
            self.errors.append("mount_prep_failed: " + str(e))
            self._proc = None
            return

        self._proc = self._start()

    def _start(self):
        container_bot_py = "/bot/bot.py"

        if USE_DOCKER:
            # Named so it can be killed directly: killing the `docker run`
            # client alone leaves the container (and a wedged bot) running.
            self._container = "fhbot_" + uuid.uuid4().hex[:12]
            cmd = [
                "docker", "run",
                "--rm",
                "--name",    self._container,
                "-i",
                "--network", "none",
                "--memory",  CONTAINER_MEMORY,
                "--memory-swap", CONTAINER_MEMORY,
                "--cpus",    CONTAINER_CPUS,
                "--read-only",
                "--security-opt", "no-new-privileges",
                "--user",    "1000:1000",
                "--pids-limit", CONTAINER_PIDS_LIMIT,
                "--tmpfs",   "/tmp:size=" + CONTAINER_TMPFS_SIZE,
                "-v",        self._mount_src + ":/bot:ro",
                "-e",        "ACTION_TIMEOUT=" + str(ACTION_TIMEOUT),
                "-e",        "WARMUP_TIMEOUT=" + str(WARMUP_TIMEOUT),
            ]
            for k, v in THREAD_CAP_ENV.items():
                cmd += ["-e", k + "=" + v]
            cmd += [
                "-e",        "BOT_PATH=" + container_bot_py,
                "-e",        "BOT_DATA_DIR=/bot/data",
                SANDBOX_IMAGE,
            ]
        else:
            cmd = [sys.executable, "-u", str(RUNNER_PATH)]

        host_bot_py = os.path.join(self._mount_src, "bot.py")
        env = {
            **os.environ,
            **THREAD_CAP_ENV,
            "BOT_PATH":       container_bot_py if USE_DOCKER else host_bot_py,
            "BOT_DATA_DIR":   "/bot/data" if USE_DOCKER else os.path.join(self._mount_src, "data"),
            "ACTION_TIMEOUT": str(ACTION_TIMEOUT),
            "WARMUP_TIMEOUT": str(WARMUP_TIMEOUT),
        }

        # stderr is discarded: nothing reads it during a match, and an unread
        # PIPE fills up and blocks a chatty bot. Set BOT_STDERR_DIR to keep it.
        stderr_dir = os.environ.get("BOT_STDERR_DIR")
        if stderr_dir:
            os.makedirs(stderr_dir, exist_ok=True)
            stderr = open(os.path.join(stderr_dir, self.bot_id + ".stderr.log"), "ab")
        else:
            stderr = subprocess.DEVNULL

        return subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            env=env,
        )

    def _send(self, obj):
        self._proc.stdin.write((json.dumps(obj) + "\n").encode())
        self._proc.stdin.flush()

    def _readline(self, timeout_s):
        """Read one protocol line, or raise TimeoutError after timeout_s."""
        deadline = time.monotonic() + timeout_s
        fd = self._proc.stdout.fileno()
        with selectors.DefaultSelector() as sel:
            sel.register(fd, selectors.EVENT_READ)
            while b"\n" not in self._buf:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not sel.select(remaining):
                    raise TimeoutError("no response within %.1fs" % timeout_s)
                chunk = os.read(fd, 65536)
                if not chunk:
                    raise EOFError("Bot process died")
                self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return line.decode()

    def _kill(self, reason):
        """A wedged or dead bot folds for the rest of the match."""
        self.dead = True
        self.errors.append(reason)
        self._terminate()

    def _terminate(self):
        if self._container:
            try:
                subprocess.run(["docker", "kill", self._container],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=15)
            except Exception:
                pass
        try:
            self._proc.kill()
        except Exception:
            pass

    def act(self, game_state):
        if self._proc is None or self.dead:
            return {"action": "fold", "error": "no_process"}
        try:
            self._send(game_state)
            line = self._readline(ACTION_TIMEOUT + HOST_GRACE_S)
            action = json.loads(line.strip())
            if not isinstance(action, dict):
                raise ValueError("response is not a JSON object")
            if "error" in action:
                self.errors.append(action["error"])
            return action
        except TimeoutError as e:
            self._kill("host_timeout: " + str(e))
            return {"action": "fold", "error": "host_timeout"}
        except (EOFError, BrokenPipeError) as e:
            self._kill("process_died: " + str(e))
            return {"action": "fold", "error": "process_died"}
        except Exception as e:
            self.errors.append(str(e))
            return {"action": "fold", "error": str(e)}

    def warmup(self):
        """One-shot 'wake up the bot before hand 1' call.

        Bots that load CFR blueprints, NN weights, or large lookup tables
        at module-import time can take 10-30s to do their first decision.
        Without this they would blow through the 2s/action timeout on
        hand 1 and auto-fold for free. The runner gives this call a
        longer alarm (see WARMUP_TIMEOUT in runner.py) and we discard
        the response — its only purpose is to let imports finish.
        """
        if self._proc is None:
            return
        try:
            self._send({"type": "warmup"})
            budget = WARMUP_TIMEOUT + HOST_GRACE_S
            if USE_DOCKER:
                budget += CONTAINER_START_GRACE_S
            self._readline(budget)  # discard
        except TimeoutError as e:
            self._kill("warmup_host_timeout: " + str(e))
        except Exception as e:
            self.errors.append("warmup_failed: " + str(e))


    def stop(self):
        if self._proc is not None:
            try:
                self._proc.stdin.close()
            except Exception:
                pass
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._terminate()
        if self._cleanup_dir and os.path.isdir(self._cleanup_dir):
            shutil.rmtree(self._cleanup_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Match runner
# ---------------------------------------------------------------------------

def _inject_match_log(state, match_log):
    if state.get("type") == "action_request":
        state["match_action_log"] = match_log[-MATCH_LOG_MAX_ENTRIES:]
    return state


def run_match(match_id, bot_paths, n_hands=400, verbose=False, seed=None,
              small_blind=SMALL_BLIND, big_blind=BIG_BLIND,
              starting_stack=STARTING_STACK):
    bot_ids = list(bot_paths.keys())
    n = len(bot_ids)
    assert 2 <= n <= MAX_PLAYERS, "Need 2-" + str(MAX_PLAYERS) + " bots, got " + str(n)

    procs   = {bid: BotProcess(bid, path) for bid, path in bot_paths.items()}
    stacks  = {bid: starting_stack for bid in bot_ids}
    hand_log = []
    match_action_log = []
    dealer = 0
    start_ts = time.time()

    # Warm-up: give every bot one untimed-by-2s call so they can finish
    # heavy imports / lookup-table loads before hand 1.
    for p in procs.values():
        p.warmup()

    try:
        for hand_num in range(n_hands):
            alive = [bid for bid in bot_ids if stacks[bid] > 0]
            if len(alive) < 2:
                break

            hand_id = match_id + "_h" + str(hand_num).zfill(4)
            hand_seed = (seed * 1000003 + hand_num) if seed is not None else None
            engine = PokerEngine(
                hand_id        = hand_id,
                bot_ids        = alive,
                dealer_seat    = dealer % len(alive),
                starting_stacks= {bid: stacks[bid] for bid in alive},
                seed           = hand_seed,
                small_blind    = small_blind,
                big_blind      = big_blind,
            )

            result = _play_hand(engine, procs, alive, match_action_log, hand_num, verbose)
            hand_log.append({"hand_num": hand_num, "hand_id": hand_id, **result})

            for bid, s in result["final_stacks"].items():
                stacks[bid] = s

            dealer += 1

            if verbose and hand_num % 25 == 0:
                _print_stacks(hand_num, n_hands, stacks)

    finally:
        for p in procs.values():
            p.stop()

    return {
        "match_id":     match_id,
        "bot_ids":      bot_ids,
        "seed":         seed,
        "small_blind":  small_blind,
        "big_blind":    big_blind,
        "starting_stack": starting_stack,
        "n_hands":      len(hand_log),
        "duration_s":   round(time.time() - start_ts, 2),
        "final_stacks": stacks,
        "chip_delta":   {bid: stacks[bid] - starting_stack for bid in bot_ids},
        "bot_errors":   {bid: procs[bid].errors for bid in bot_ids},
        "hands":        hand_log,
    }


def _play_hand(engine, procs, active_bots, match_action_log, hand_num, verbose):
    state = _inject_match_log(engine.start_hand(), match_action_log)
    steps = 0

    while state.get("type") == "action_request":
        seat   = state["seat_to_act"]
        bot_id = active_bots[seat]
        action = procs[bot_id].act(state)

        if verbose:
            print("  [" + bot_id + "] " + str(action), file=sys.stderr)

        next_state = engine.apply_action(seat, action)

        # Log what the engine applied (after snapping/conversion), not the
        # raw request, so opponent models see the real betting.
        applied = engine.action_log[-1]
        match_action_log.append({
            "hand_num": hand_num,
            "seat":     seat,
            "bot_id":   bot_id,
            "action":   applied["action"],
            "amount":   applied["amount"],
        })

        state = _inject_match_log(next_state, match_action_log)
        steps += 1

        if steps > 1000:
            raise RuntimeError("Hand exceeded 1000 steps: " + engine.hand_id)

    return state


def _print_stacks(hand_num, total, stacks):
    print("\n  === Hand " + str(hand_num) + "/" + str(total) + " ===", file=sys.stderr)
    for bid, s in sorted(stacks.items(), key=lambda x: -x[1]):
        bar = "X" * (s // 1000)
        print("  " + bid.ljust(20) + " " + str(s).rjust(7) + "  " + bar, file=sys.stderr)


# ---------------------------------------------------------------------------
# CLI entrypoint for local testing
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run a Fullhouse match locally")
    parser.add_argument("bots", nargs="+",
                        help="Paths to bot.py files, bot directories, or bot.zip archives")
    parser.add_argument("--hands", type=int, default=400)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--json", action="store_true", help="Output result as JSON (for worker)")
    parser.add_argument("--match-id", default=None)
    parser.add_argument("--seed", type=int, default=None,
                        help="Deterministic seed — same seed + same bots = same cards. Useful for reproducing a match locally.")
    parser.add_argument("--small-blind", type=int, default=SMALL_BLIND)
    parser.add_argument("--big-blind", type=int, default=BIG_BLIND)
    parser.add_argument("--stack", type=int, default=STARTING_STACK,
                        help="Starting stack in chips (default 10000 = 100 BB at 50/100)")
    args = parser.parse_args()

    paths = {}
    for i, path in enumerate(args.bots):
        pp = Path(path)
        suffix = pp.suffix
        # Default bot_id from filename, but if it would collide (very common
        # with the "bots/<name>/bot.py" layout where every stem is "bot"),
        # fall back to the parent directory name. If that also collides
        # (e.g. the same bot path passed twice for self-play testing),
        # append a numeric suffix so every entry is unique.
        if suffix in (".py", ".zip"):
            bot_id = pp.stem
        else:
            bot_id = pp.name or "bot_" + str(i)
        if bot_id in paths or bot_id in ("bot",):
            bot_id = pp.parent.name or ("bot_" + str(i))
        base = bot_id or "bot_" + str(i)
        bot_id = base
        n = 2
        while bot_id in paths:
            bot_id = base + "_" + str(n)
            n += 1
        paths[bot_id] = path

    match_id = args.match_id or os.environ.get("MATCH_ID") or "local_" + uuid.uuid4().hex[:8]

    if not args.json:
        print("Starting match " + match_id + " with " + str(len(paths)) + " bots, " + str(args.hands) + " hands\n")

    result = run_match(match_id, paths, n_hands=args.hands, verbose=args.verbose, seed=args.seed,
                       small_blind=args.small_blind, big_blind=args.big_blind,
                       starting_stack=args.stack)

    if args.json:
        print(json.dumps({
            "match_id":     result["match_id"],
            "seed":         result["seed"],
            "n_hands":      result["n_hands"],
            "duration_s":   result["duration_s"],
            "final_stacks": result["final_stacks"],
            "chip_delta":   result["chip_delta"],
            "bot_errors":   result["bot_errors"],
        }))
        sys.exit(0)

    print("\n" + "=" * 50)
    print("Match complete in " + str(result["duration_s"]) + "s")
    print("=" * 50)
    print("Bot".ljust(25) + " " + "Final Stack".rjust(12) + " " + "Delta".rjust(10))
    print("-" * 50)
    for bid in sorted(result["bot_ids"], key=lambda b: -result["final_stacks"][b]):
        delta = result["chip_delta"][bid]
        sign  = "+" if delta >= 0 else ""
        print(bid.ljust(25) + " " + str(result["final_stacks"][bid]).rjust(12) + " " + sign + str(delta).rjust(9))
    print("\nHands played: " + str(result["n_hands"]))

    errs = {b: e for b, e in result["bot_errors"].items() if e}
    if errs:
        print("\nBot errors: " + str(errs))
