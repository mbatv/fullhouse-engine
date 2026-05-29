"""
Fullhouse Hackathon — Local Demo
Run: python demo.py
Open: http://localhost:5001

No Docker, Redis, or Supabase needed.
Runs real matches using the actual game engine and shows results live.
"""

import sys, os, json, time, threading, uuid, random
sys.path.insert(0, os.path.dirname(__file__))

from flask import Flask, Response, jsonify, render_template_string
from sandbox.match import run_match
from engine.tournament import swiss_pairing, compute_standings, select_finalists

app = Flask(__name__)

# ---------------------------------------------------------------------------
# State (in-memory for demo)
# ---------------------------------------------------------------------------

BOT_PATHS = {
    "The Aggressor":    "bots/aggressor/bot.py",
    "The Mathematician":"bots/mathematician/bot.py",
    "The Shark":        "bots/shark/bot.py",
    "Template Bot A":   "bots/template/bot.py",
    "Pot-Odds Bot B":   "bots/ref_bot_2/bot.py",
    "Template Bot C":   "bots/template/bot.py",
}

state = {
    "log":        [],   # event log (SSE stream)
    "standings":  [],   # current leaderboard
    "hands":      [],   # last match hand history
    "running":    False,
    "round":      0,
}

log_lock = threading.Lock()

def emit(msg, kind="info"):
    with log_lock:
        state["log"].append({"t": time.time(), "msg": msg, "kind": kind})

# ---------------------------------------------------------------------------
# HTML — single-page terminal UI
# ---------------------------------------------------------------------------

PAGE = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Fullhouse — Local Demo</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: #080c08; color: #00ff41; font-family: 'Share Tech Mono', 'Courier New', monospace; font-size: 14px; }
@import url('https://fonts.googleapis.com/css2?family=Share+Tech+Mono&display=swap');

.grid { display: grid; grid-template-columns: 1fr 1fr; grid-template-rows: auto 1fr auto; gap: 1px; min-height: 100vh; background: #1a2e1a; }
.panel { background: #080c08; padding: 16px; overflow: hidden; display: flex; flex-direction: column; }
.header { grid-column: 1 / -1; border-bottom: 1px solid #00ff41; padding: 12px 16px; display: flex; align-items: center; gap: 24px; }
.logo { font-size: 18px; letter-spacing: 2px; }
.blink { animation: blink 1s step-end infinite; }
@keyframes blink { 50% { opacity: 0; } }

h2 { font-size: 12px; letter-spacing: 3px; color: #00cc33; margin-bottom: 12px; text-transform: uppercase; }

/* Log */
#log { flex: 1; overflow-y: auto; font-size: 12px; line-height: 1.8; }
#log div { padding: 1px 0; }
.info  { color: #00ff41; }
.win   { color: #00ffcc; }
.err   { color: #ff4444; }
.dim   { color: #3a6e3a; }
.bold  { color: #ffffff; font-weight: bold; }

/* Leaderboard */
#board { flex: 1; overflow-y: auto; }
.row { display: grid; grid-template-columns: 28px 1fr 90px 70px; gap: 8px; padding: 5px 0; border-bottom: 1px solid #0d1f0d; align-items: center; font-size: 13px; }
.row.header { color: #3a6e3a; font-size: 11px; letter-spacing: 1px; }
.rank  { color: #3a6e3a; }
.rank.top { color: #00ff41; }
.delta.pos { color: #00ffcc; }
.delta.neg { color: #ff4444; }
.bar-wrap { background: #0d1f0d; height: 4px; border-radius: 2px; margin-top: 2px; }
.bar { height: 4px; background: #00ff41; border-radius: 2px; transition: width 0.5s; }

/* Controls */
.controls { margin-top: 12px; display: flex; gap: 10px; flex-wrap: wrap; }
button { background: transparent; border: 1px solid #00ff41; color: #00ff41; font-family: inherit; font-size: 12px; padding: 6px 16px; cursor: pointer; letter-spacing: 1px; transition: all 0.15s; }
button:hover { background: #00ff41; color: #080c08; }
button:disabled { opacity: 0.3; cursor: not-allowed; }
button:disabled:hover { background: transparent; color: #00ff41; }

.status-pill { font-size: 11px; letter-spacing: 2px; padding: 3px 10px; border: 1px solid #3a6e3a; color: #3a6e3a; }
.status-pill.running { border-color: #00ff41; color: #00ff41; animation: pulse 1s ease-in-out infinite; }
@keyframes pulse { 50% { opacity: 0.5; } }

/* Hand replay */
#replay { max-height: 60vh; overflow-y: auto; font-size: 12px; }
details.replay-wrap > summary { cursor: pointer; list-style: none; color: #00cc33; font-size: 12px; letter-spacing: 3px; text-transform: uppercase; margin-bottom: 12px; user-select: none; }
details.replay-wrap > summary::-webkit-details-marker { display: none; }
details.replay-wrap > summary::before { content: '▾ '; color: #00ff41; }
details.replay-wrap:not([open]) > summary::before { content: '▸ '; }
.hand-card { border: 1px solid #1a2e1a; padding: 8px 10px; margin-bottom: 6px; }
.hand-card:hover { border-color: #3a6e3a; }
.hand-meta { color: #3a6e3a; font-size: 11px; margin-bottom: 4px; }
.action-line { color: #00cc33; }
.action-line.fold { color: #3a6e3a; }
.action-line.raise { color: #00ffcc; }
.community { color: #ffffff; letter-spacing: 2px; }
</style>
</head>
<body>
<div class="grid">
  <div class="header">
    <span class="logo">FULLHOUSE<span class="blink">_</span></span>
    <span class="status-pill" id="status">IDLE</span>
    <span style="color:#3a6e3a;font-size:11px" id="round-label"></span>
  </div>

  <div class="panel">
    <h2>Event log</h2>
    <div id="log"></div>
    <div class="controls">
      <button id="btn-single" onclick="runSingle()">Run 1 match</button>
      <button id="btn-tournament" onclick="runTournament()">Run tournament</button>
      <button onclick="clearLog()">Clear log</button>
    </div>
  </div>

  <div class="panel">
    <h2>Leaderboard</h2>
    <div id="board">
      <div class="row header">
        <span>#</span><span>Bot</span><span>Chip Δ</span><span>Matches</span>
      </div>
    </div>
  </div>

  <div class="panel" style="grid-column:1/-1;">
    <details class="replay-wrap" open>
      <summary>Last match — hand replay <span id="hand-count" style="color:#3a6e3a"></span> <span style="color:#3a6e3a;text-transform:none;letter-spacing:0;font-size:11px">(click to collapse)</span></summary>
      <div id="replay"></div>
    </details>
  </div>
</div>

<script>
let running = false;

// SSE event log
const evtSrc = new EventSource('/stream');
evtSrc.onmessage = e => {
  const { msg, kind } = JSON.parse(e.data);
  addLog(msg, kind);
};

function addLog(msg, kind='info') {
  const el = document.getElementById('log');
  const d = document.createElement('div');
  d.className = kind;
  d.textContent = '> ' + msg;
  el.appendChild(d);
  el.scrollTop = el.scrollHeight;
}

function clearLog() {
  document.getElementById('log').innerHTML = '';
}

function setRunning(v) {
  running = v;
  document.getElementById('btn-single').disabled = v;
  document.getElementById('btn-tournament').disabled = v;
  const pill = document.getElementById('status');
  pill.textContent = v ? 'RUNNING' : 'IDLE';
  pill.className = 'status-pill' + (v ? ' running' : '');
}

async function runSingle() {
  if (running) return;
  setRunning(true);
  try {
    const res = await fetch('/run/match', { method: 'POST' });
    const data = await res.json();
    updateBoard(data.standings);
    updateReplay(data.hands);
  } catch (err) {
    addLog('match failed: ' + (err && err.message ? err.message : err), 'err');
  } finally {
    setRunning(false);
  }
}

async function runTournament() {
  if (running) return;
  setRunning(true);
  try {
    const res = await fetch('/run/tournament', { method: 'POST' });
    const data = await res.json();
    updateBoard(data.standings);
    document.getElementById('round-label').textContent =
      `Round ${data.round} complete — ${data.finalists} finalists selected`;
  } catch (err) {
    addLog('tournament failed: ' + (err && err.message ? err.message : err), 'err');
  } finally {
    setRunning(false);
  }
}

function updateBoard(standings) {
  const board = document.getElementById('board');
  board.innerHTML = `<div class="row header"><span>#</span><span>Bot</span><span>Chip Δ</span><span>Matches</span></div>`;
  const max = Math.max(...standings.map(s => Math.abs(s.cumulative_delta)), 1);
  standings.forEach((s, i) => {
    const delta = s.cumulative_delta;
    const pct = Math.min(Math.abs(delta) / max * 100, 100);
    const sign = delta >= 0 ? '+' : '';
    const dClass = delta >= 0 ? 'pos' : 'neg';
    const rClass = i < 3 ? 'top' : '';
    board.innerHTML += `
      <div class="row">
        <span class="rank ${rClass}">${i+1}</span>
        <span>${s.bot_id}</span>
        <span class="delta ${dClass}">${sign}${delta.toLocaleString()}</span>
        <span style="color:#3a6e3a">${s.matches_played}</span>
      </div>
      <div style="padding:0 0 4px 36px">
        <div class="bar-wrap"><div class="bar" style="width:${pct}%;${delta<0?'background:#ff4444':''}"></div></div>
      </div>`;
  });
}

function updateReplay(hands) {
  if (!hands || hands.length === 0) return;
  const el = document.getElementById('replay');
  el.innerHTML = '';
  document.getElementById('hand-count').textContent = `(${hands.length} hands)`;

  // Show all hands (newest first) — inner #replay scrolls via max-height + overflow-y
  const show = hands.slice().reverse();
  show.forEach(h => {
    const r = h.result || h;
    const winner = r.winners?.map(w => w.bot_id).join(', ') || '?';
    const community = r.community_cards?.join(' ') || '—';
    const actions = (r.action_log || [])
      .filter(a => !['small_blind','big_blind'].includes(a.action))
      .slice(-6);

    let html = `<div class="hand-card">`;
    html += `<div class="hand-meta">Hand #${h.hand_num+1} · ${r.street} · pot ${r.pot?.toLocaleString()} · winner: ${winner}</div>`;
    if (community !== '—') html += `<div class="community">${community}</div>`;
    actions.forEach(a => {
      const cls = a.action === 'fold' ? 'fold' : a.action === 'raise' ? 'raise' : '';
      const amt = a.amount ? ` ${a.amount.toLocaleString()}` : '';
      html += `<div class="action-line ${cls}">seat${a.seat} ${a.action}${amt}</div>`;
    });
    html += `</div>`;
    el.innerHTML += html;
  });
}
</script>
</body>
</html>"""

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template_string(PAGE)


@app.route("/stream")
def stream():
    """SSE endpoint — pushes log events to the browser in real time."""
    def generate():
        last = 0
        while True:
            with log_lock:
                new = state["log"][last:]
                last = len(state["log"])
            for entry in new:
                yield f"data: {json.dumps(entry)}\n\n"
            time.sleep(0.2)
    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/run/match", methods=["POST"])
def run_single_match():
    """Run one 6-player match and return updated standings."""
    bot_ids  = list(BOT_PATHS.keys())
    match_id = f"demo_{uuid.uuid4().hex[:8]}"

    emit(f"Starting match {match_id}...", "dim")
    t0 = time.time()

    result = run_match(match_id, BOT_PATHS, n_hands=150, verbose=False)
    elapsed = time.time() - t0

    emit(f"Match complete in {elapsed:.1f}s", "dim")

    # Update standings
    all_results = []
    for bid, delta in result["chip_delta"].items():
        all_results.append({"bot_id": bid, "bot_path": BOT_PATHS.get(bid,""), "chip_delta": delta})

    prev = {s["bot_id"]: s for s in state["standings"]}
    new_standings = compute_standings(
        [{**s, "chip_delta": s["cumulative_delta"]} for s in state["standings"]] + all_results
    )
    state["standings"] = new_standings

    # Log results
    sorted_res = sorted(result["chip_delta"].items(), key=lambda x: -x[1])
    for bid, delta in sorted_res:
        sign = "+" if delta >= 0 else ""
        kind = "win" if delta > 0 else "err" if delta < 0 else "dim"
        emit(f"  {bid:22s} {sign}{delta:,}", kind)

    state["hands"] = result.get("hands", [])

    return jsonify({
        "standings": new_standings,
        "hands": result.get("hands", []),
    })


@app.route("/run/tournament", methods=["POST"])
def run_tournament():
    """Run a 3-round Swiss tournament across all bots."""
    bot_list = [{"bot_id": bid, "bot_path": path, "cumulative_delta": 0, "matches_played": 0}
                for bid, path in BOT_PATHS.items()]

    state["standings"] = []
    all_results = []

    for rnd in range(1, 4):
        state["round"] = rnd
        emit(f"=== ROUND {rnd} ===", "bold")

        standings_for_pairing = compute_standings(all_results) if all_results else bot_list
        tables = swiss_pairing(standings_for_pairing, table_size=min(6, len(bot_list)))

        emit(f"  {len(tables)} table(s) this round", "dim")

        for t_idx, table in enumerate(tables):
            bot_paths_for_match = {b["bot_id"]: b["bot_path"] for b in table}
            match_id = f"t_r{rnd}_t{t_idx}"

            emit(f"  Table {t_idx+1}: {', '.join(bot_paths_for_match.keys())}", "dim")

            result = run_match(match_id, bot_paths_for_match, n_hands=150)

            for bid, delta in result["chip_delta"].items():
                all_results.append({
                    "bot_id": bid,
                    "bot_path": BOT_PATHS.get(bid, ""),
                    "chip_delta": delta,
                })
                sign = "+" if delta >= 0 else ""
                kind = "win" if delta > 0 else "err" if delta < 0 else "dim"
                emit(f"    {bid:22s} {sign}{delta:,}", kind)

    final_standings = compute_standings(all_results)
    state["standings"] = final_standings

    finalists = select_finalists(final_standings, n=3)
    emit("=== FINALISTS ===", "bold")
    for i, f in enumerate(finalists):
        emit(f"  #{i+1} {f['bot_id']}  ({f['cumulative_delta']:+,})", "win")

    return jsonify({
        "standings": final_standings,
        "round": 3,
        "finalists": len(finalists),
        "hands": state.get("hands", []),
    })


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("\n" + "="*50)
    print("  FULLHOUSE HACKATHON — LOCAL DEMO")
    print("="*50)
    port = int(os.environ.get("DEMO_PORT", "5001"))
    print(f"  Open:  http://localhost:{port}")
    print("  Bots:  ", ", ".join(BOT_PATHS.keys()))
    print("="*50 + "\n")
    app.run(debug=False, threaded=True, port=port)
