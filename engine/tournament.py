"""
Swiss-tournament pairing + standings + finalist selection.

Pure-Python helpers used by the demo server and any local tournament harness.
The production match queue (BullMQ workers) ships its own copy in the private
ops repo; this module is the canonical reference for participants who want to
run their own multi-bot bake-off locally.
"""


def swiss_pairing(standings, table_size=6):
    """
    Pair bots into tables. `standings` is a list of dicts with bot_id, bot_path,
    cumulative_delta. Returns a list of tables (each table is a list of bot dicts).

    When the field does not divide evenly, it is split into the fewest tables
    of at most `table_size` seats, with sizes differing by at most one (larger
    tables first). E.g. 64 bots at 6-max -> 9 tables of 6 and 2 tables of 5.
    The 2026 reference instead folded the remainder into the last table, which
    could produce tables above the engine's seat limit (64 -> a 10-seat table).
    """
    sorted_bots = sorted(standings, key=lambda b: -b.get("cumulative_delta", 0))
    if not sorted_bots:
        return []

    n_tables = -(-len(sorted_bots) // table_size)
    base, extra = divmod(len(sorted_bots), n_tables)

    tables = []
    i = 0
    for t in range(n_tables):
        size = base + (1 if t < extra else 0)
        tables.append(sorted_bots[i:i + size])
        i += size

    return tables


def compute_standings(all_results):
    """
    `all_results`: list of {bot_id, bot_path, chip_delta}.
    Returns a sorted list of {bot_id, bot_path, cumulative_delta,
    matches_played, best_match_delta}, ranked by:

      1. cumulative_delta (DESC)             — primary: total chips won
      2. matches_played   (ASC)              — fewer matches with same chips = stronger
      3. best_match_delta (DESC)             — best single-match performance
      4. bot_id           (ASC)              — deterministic alphabetic last resort

    The matches_played tiebreaker rewards bots that scored a high
    cumulative chip delta over fewer matches (less variance exposure)
    over bots that ground it out across more rounds. Standard practice
    in chess-style Swiss tournaments adapted to chip scoring.
    """
    totals = {}
    for r in all_results:
        bid = r["bot_id"]
        delta = r["chip_delta"]
        if bid not in totals:
            totals[bid] = {
                "bot_id":           bid,
                "bot_path":         r.get("bot_path", ""),
                "cumulative_delta": 0,
                "matches_played":   0,
                "best_match_delta": delta,
            }
        totals[bid]["cumulative_delta"] += delta
        totals[bid]["matches_played"]   += 1
        if delta > totals[bid]["best_match_delta"]:
            totals[bid]["best_match_delta"] = delta

    return sorted(
        totals.values(),
        key=lambda b: (
            -b["cumulative_delta"],
             b["matches_played"],
            -b["best_match_delta"],
             b["bot_id"],
        ),
    )


def select_finalists(standings, n=64):
    """Top-n bots by cumulative chip delta with documented tiebreakers
    (see compute_standings). Default n=64 matches the qualifier cut."""
    return standings[:n]
