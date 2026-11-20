# Fullhouse Engine

> The UK's first quantitative poker bot competition — 1-5 June 2026, London
> **£4,000 prize pool · Sponsored by Quadrature Capital**

Build a Python bot that plays No-Limit Texas Hold'em. This repo has everything you need to write and test your bot locally — the full game engine, sandbox runner, reference bots, and validator.

---

## How it works

You submit one file — `bot.py` — with one function:

```python
def decide(game_state: dict) -> dict:
    # your entire strategy goes here
    return {"action": "call"}
```

The engine calls `decide()` once per action. You get the full game state — your cards, community cards, pot size, stack sizes, betting history, position. You return one action. That's it.

---

## Getting started

```bash
git clone https://github.com/uzlez/fullhouse-engine
cd fullhouse-engine

# eval7 needs Cython<3 at build time and won't pick it up via build isolation
pip3 install "Cython<3"
pip3 install --no-build-isolation eval7==0.1.7
pip3 install flask numpy scipy treys scikit-learn

python3 demo.py
```

Open `http://localhost:5001` — you'll see 6 reference bots playing each other live with a real-time leaderboard and hand replay.

> **macOS users**: we use port 5001 instead of the Flask default 5000 because macOS Monterey+ binds port 5000 to AirPlay Receiver. Override with `DEMO_PORT=8080 python3 demo.py` if 5001 is taken too.

> Hitting a build error on `eval7`? You're not alone — modern pip's build
> isolation breaks it. The two-step install above is the workaround. We have
> a `Makefile` target too: `make install`.

---

## Writing your bot

Copy the template and edit the `decide()` function:

```bash
cp -r bots/template bots/mybot
# edit bots/mybot/bot.py
```

**Game state your bot receives:**

| Key | Type | Description |
|-----|------|-------------|
| `your_cards` | `list[str]` | Your two hole cards e.g. `["As", "Kh"]` |
| `community_cards` | `list[str]` | Board cards e.g. `["7d", "Tc", "2s"]` |
| `street` | `str` | `preflop` / `flop` / `turn` / `river` |
| `pot` | `int` | Total chips in the pot |
| `your_stack` | `int` | Your remaining chips |
| `amount_owed` | `int` | Chips needed to call (0 = free check) |
| `can_check` | `bool` | True when no bet to call |
| `current_bet` | `int` | Highest bet this street |
| `min_raise_to` | `int` | Minimum legal raise total |
| `can_raise` | `bool` | False when you may only call or fold (e.g. facing a short all-in after you already acted) |
| `small_blind` / `big_blind` | `int` | Blind sizes for this table |
| `players` | `list` | Public info on all seats |
| `action_log` | `list` | Every action taken this hand |

**Valid return values:**

```python
{"action": "fold"}
{"action": "check"}                       # only when can_check is True
{"action": "call"}
{"action": "raise", "amount": 1200}       # amount = total bet, not raise-by
{"action": "all_in"}
```

Invalid or missing actions default to fold. Raises below the minimum are snapped up automatically. A raise or all-in when `can_raise` is False is applied as a call.

**Rules:**
- 2 seconds to return an action or your bot auto-folds
- No network calls during gameplay
- No file writes ever; reads from `data/` allowed at module-import time only
- **768 MB RAM**, 0.5 CPU core per bot
- Crashes and exceptions auto-fold for that hand — your bot stays in the tournament

**Available libraries:** `eval7` `numpy` `scipy` `treys` `scikit-learn` — request others before the event

### Not allowed (will get your bot disqualified)

The sandbox blocks most of these at the OS level — but listing them explicitly so there's no ambiguity:

- **No external API calls of any kind.** No `requests` to Claude, OpenAI, Anthropic, Google, or any other LLM/AI service. No webhook callbacks, no DNS lookups, no `socket`. The container has `--network none` so these fail anyway, but doing them on purpose is grounds for disqualification.
- **No reading another bot's code or hole cards.** You only see what's in `state["your_cards"]`. Don't try to access opponents' files, scrape `/proc`, or use reflection to peek at the runner's memory. The hole-card data isn't in your container's process memory — but trying counts as cheating.
- **No file writes during gameplay.** The filesystem is read-only at runtime. `data/` is read-only too. Trying to `os.system`, `subprocess`, or `open(..., "w")` will be blocked or get you DQ'd.
- **No threading or async tricks to dodge the 2 s/action timeout.** A `decide()` that overruns is answered with a fold; it cannot be interrupted, so until it finishes every further request is also folded, and a bot that stops responding altogether is killed and folds for the rest of the match. Spawning background threads to keep computing past the deadline counts as fraud, not strategy.
- **No collusion.** If you and a friend both register, your bots must play independently. Coordinated soft-play, chip-dumping, or sharing live game state between submissions is grounds for both bots being DQ'd and forfeit of any prize.
- **No crypto-mining or resource abuse.** The 768 MB / 0.5 CPU limits will OOM-kill abusive bots, but doing it on purpose (e.g., to slow opponents) is bannable.
- **No reflection escape attempts.** `__import__('socket')`, `getattr(__builtins__, 'open')`, `eval()`, `exec()`, `compile()` are all flagged by the validator and rejected. Don't try clever obfuscation either — `__import__('so'+'cket')` etc. fails the AST check at submission time.
- **No external compute.** You may not use the bot.py to call out to a server you control (it can't anyway, see point 1) or queue work to be done elsewhere. The bot must make its own decisions inside its own container.

**TL;DR:** treat your container as if it's the entire universe. If it's not in `requirements.txt` or the Python stdlib, and you can't load it from your own `data/` at import time, it doesn't exist.

### Submission formats

Pick whichever fits your bot:

| Format | Use when |
|---|---|
| `bot.py` (single file) | Simple bot, no large lookup tables |
| `bot.zip` containing `bot.py` + optional `data/` | You ship a CFR blueprint, neural-net weights, equity table, etc. |

`data/` constraints:
- ≤ 200 MB total
- No `.py` files inside (use `bot.py` for code)
- Read-only at runtime, accessed via `os.environ["BOT_DATA_DIR"]`
- Loaded **at module-import time only** — gameplay actions still must respond in 2 s

```python
# bot.py
import os, numpy as np
DATA_DIR = os.environ.get("BOT_DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
BLUEPRINT = np.load(os.path.join(DATA_DIR, "blueprint.npz"))   # loaded once at import

def decide(state):
    # use BLUEPRINT to make decisions — no file I/O here
    ...
```

---

## Submitting your bot

Once your bot validates cleanly locally (`make validate BOT=bots/mybot/bot.py`), upload it at **[portal.fullhousehackathon.com](https://portal.fullhousehackathon.com)**.

**Deadline:** **31 May 2026, 23:59 UTC.** Late submissions don't enter the qualifier.

The portal runs `sandbox/validator.py` against your upload and rejects anything that fails — so a green local validation is the bar to clear. Submit either a single `bot.py` or a `.zip` archive with `bot.py` at the root plus an optional `data/` directory (see [Submission formats](#submission-formats) above for limits).

If validation passes, your bot is queued for the **1 June qualifier**. You can re-upload up to the deadline; only your most recent successful submission counts.

**Patch window:** between Day 1 (qualifier) and Day 5 (finals), you can replace your bot with an updated version once. Same submission flow; your bot ID stays the same across all rounds.

---

## Running matches

```bash
# single match, 400 hands (qualifier length)
python3 sandbox/match.py bots/mybot/bot.py bots/shark/bot.py --hands 400

# full tournament simulation (3 Swiss rounds)
# use the demo UI at http://localhost:5001
```

---

## Tournament format

**1 Jun — Online qualifier**
All bots play in a Swiss-system tournament (multiple rounds, 400 hands per match, 6-bot tables). Bots are paired by similar standing after each round. Ranking is by cumulative chip delta. **Top 64 advance** to the finals.

**2 Jun — Patch window**
Hand histories from your D1 matches are downloadable as JSON. You can submit one updated bot before D5.

**5 Jun — Finals night, UCL East**
Top 64 play a single-elimination bracket live on stage. Winner takes the prize pool.

> **Why 400 hands?** Lower variance per match. With only 200 hands, "overbet a lot" can be +EV in tournament terms (you can lose −10k max but gain up to +50k, and only top 64 advance). 400 hands tilts the field back toward skill.

---

## Reference bots

Five bots are included to test against:

| Bot | Strategy |
|-----|----------|
| `bots/template/bot.py` | Pocket pairs + basic pot odds |
| `bots/aggressor/bot.py` | Raises constantly regardless of hand |
| `bots/mathematician/bot.py` | Calls only when getting 3:1 pot odds |
| `bots/shark/bot.py` | Tight preflop, position-aware, value bets |
| `bots/ref_bot_2/bot.py` | Pot-odds caller — heuristic reference baseline |

---

## Repo structure

```
engine/         Game engine — NLHE rules, hand evaluation, chip tracking
sandbox/        Validator + local match runner
bots/           Reference bots and starter template
tests/          Engine unit tests
demo.py         Quick local demo
```

---

## Tech stack

| Layer | Technology |
|-------|------------|
| Game engine | Python 3.10 (eval7 needs `longintrepr.h`, removed in 3.11+) |
| Hand evaluation | eval7 (same as MIT Pokerbots) |
| Bot isolation | 2s time limit, no network, no file I/O |

---

## Event details

**Fullhouse Hackathon** — 1 June 2026, London
Prize pool: £4,000+· Lead sponsor: Quadrature Capital

[fullhousehackathon.com](https://fullhousehackathon.com)

---

## Questions

Open an issue or reach out via [fullhousehackathon.com](https://fullhousehackathon.com).
