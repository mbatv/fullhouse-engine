"""Tests for Swiss pairing and standings."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from engine.game import MAX_PLAYERS
from engine.tournament import swiss_pairing, compute_standings


def field(n):
    return [{"bot_id": f"b{i:03d}", "cumulative_delta": -i} for i in range(n)]


def test_even_field_uses_full_tables():
    assert [len(t) for t in swiss_pairing(field(60))] == [6] * 10


def test_uneven_field_never_exceeds_table_size():
    for n in range(2, 200):
        sizes = [len(t) for t in swiss_pairing(field(n))]
        assert sum(sizes) == n
        assert max(sizes) <= 6
        assert max(sizes) - min(sizes) <= 1
        assert max(sizes) <= MAX_PLAYERS


def test_64_bots_split_into_sixes_and_fives():
    assert [len(t) for t in swiss_pairing(field(64))] == [6] * 9 + [5] * 2


def test_pairing_keeps_standings_order():
    tables = swiss_pairing(field(13))
    flat = [b["bot_id"] for t in tables for b in t]
    assert flat == [f"b{i:03d}" for i in range(13)]


def test_standings_tiebreaks():
    results = [
        {"bot_id": "x", "chip_delta": 100},
        {"bot_id": "y", "chip_delta": 50},
        {"bot_id": "y", "chip_delta": 50},
        {"bot_id": "z", "chip_delta": 100},
    ]
    order = [s["bot_id"] for s in compute_standings(results)]
    assert order == ["x", "z", "y"]
