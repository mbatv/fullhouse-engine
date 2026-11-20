"""Regression tests for betting-rule edge cases and per-table configuration."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import eval7
from engine.game import PokerEngine


def play_out_checking(eng, state):
    while state.get("type") == "action_request":
        state = eng.apply_action(state["seat_to_act"], {"action": "call"})
    return state


def test_short_all_in_must_be_called_by_prior_actors():
    # Dealer 0, SB 1, BB 2. A raises to 300, B calls, C goes all-in for 350.
    eng = PokerEngine("h", ["A", "B", "C"], dealer_seat=0,
                      starting_stacks={"A": 10000, "B": 10000, "C": 350}, seed=1)
    eng.start_hand()
    eng.apply_action(0, {"action": "raise", "amount": 300})
    eng.apply_action(1, {"action": "call"})
    state = eng.apply_action(2, {"action": "all_in"})

    assert state["street"] == "preflop"
    assert state["seat_to_act"] == 0
    assert state["amount_owed"] == 50
    assert state["can_raise"] is False

    # A re-raise request is converted to a call: action is not reopened.
    state = eng.apply_action(0, {"action": "raise", "amount": 2000})
    assert eng.action_log[-1] == {"seat": 0, "action": "call", "amount": 50}
    assert state["seat_to_act"] == 1 and state["can_raise"] is False

    state = eng.apply_action(1, {"action": "call"})
    assert state["street"] == "flop"
    assert [p.total_invested for p in eng.players] == [350, 350, 350]


def test_stacked_short_all_ins_reopen_when_they_sum_to_a_full_raise():
    # Dealer 0, SB 1, BB 2, UTG 3. D raises to 300 (a 200 raise); A calls.
    # B (450 total) and C (560 total) each make short all-ins; together they
    # raise D's and A's bet by 260 >= 200, so both may raise again.
    eng = PokerEngine("h", ["A", "B", "C", "D"], dealer_seat=0,
                      starting_stacks={"A": 10000, "B": 450, "C": 560, "D": 10000},
                      seed=1)
    eng.start_hand()
    eng.apply_action(3, {"action": "raise", "amount": 300})
    eng.apply_action(0, {"action": "call"})
    eng.apply_action(1, {"action": "all_in"})       # to 450: +150, short
    state = eng.apply_action(2, {"action": "all_in"})  # to 560: +110, short

    assert state["seat_to_act"] == 3
    assert state["can_raise"] is True
    state = eng.apply_action(3, {"action": "raise", "amount": 1200})
    assert eng.action_log[-1]["action"] == "raise"


def test_single_short_all_in_after_full_raise_keeps_reraise_for_earlier_actor():
    # Postflop: A bets 100, B raises to 300 (full), C short all-in to 400.
    # A acted at 100, so it faces a full raise and may re-raise; B may not.
    eng = PokerEngine("h", ["A", "B", "C"], dealer_seat=2,
                      starting_stacks={"A": 10000, "B": 10000, "C": 500}, seed=2)
    state = eng.start_hand()
    while state["street"] == "preflop":
        state = eng.apply_action(state["seat_to_act"], {"action": "call"})
    assert state["seat_to_act"] == 0
    eng.apply_action(0, {"action": "raise", "amount": 100})
    eng.apply_action(1, {"action": "raise", "amount": 300})
    state = eng.apply_action(2, {"action": "all_in"})   # C has 400 left

    assert state["seat_to_act"] == 0 and state["can_raise"] is True
    state = eng.apply_action(0, {"action": "call"})
    assert state["seat_to_act"] == 1 and state["can_raise"] is False


def test_short_big_blind_post_still_requires_full_big_blind_to_call():
    eng = PokerEngine("h", ["A", "B", "C"], dealer_seat=0,
                      starting_stacks={"A": 10000, "B": 10000, "C": 40}, seed=3)
    state = eng.start_hand()
    assert eng.players[2].is_all_in
    assert state["seat_to_act"] == 0
    assert state["amount_owed"] == 100


def test_odd_chip_goes_to_first_winner_left_of_button():
    # Blinds 25/50: A limps, B (SB) folds, C checks -> pot 125, chopped.
    eng = PokerEngine("h", ["A", "B", "C"], dealer_seat=0, seed=4,
                      small_blind=25, big_blind=50)
    eng.start_hand()
    c = eval7.Card
    eng.players[0].hole_cards = [c("2c"), c("3d")]
    eng.players[1].hole_cards = [c("4c"), c("5d")]
    eng.players[2].hole_cards = [c("2d"), c("3c")]
    eng._deck_cards[eng._deck_idx:eng._deck_idx + 5] = [
        c("As"), c("Ks"), c("Qs"), c("Js"), c("Ts")]

    eng.apply_action(0, {"action": "call"})
    eng.apply_action(1, {"action": "fold"})
    state = eng.apply_action(2, {"action": "check"})
    result = play_out_checking(eng, state)

    # The folded SB's 25 creates a 75-chip level and a 50-chip level.
    won = {}
    for w in result["winners"]:
        won[w["bot_id"]] = won.get(w["bot_id"], 0) + w["amount"]
    assert won == {"C": 63, "A": 62}


def test_ten_max_hand_runs_and_conserves_chips():
    ids = [f"b{i}" for i in range(10)]
    eng = PokerEngine("h", ids, dealer_seat=3, seed=5)
    state = eng.start_hand()
    assert len(state["players"]) == 10
    result = play_out_checking(eng, state)
    assert result["type"] == "hand_complete"
    assert sum(result["final_stacks"].values()) == 10 * 10_000


def test_custom_blinds_and_stacks():
    eng = PokerEngine("h", ["A", "B", "C"], dealer_seat=0, seed=6,
                      small_blind=10, big_blind=20, starting_stack=500)
    state = eng.start_hand()
    assert state["big_blind"] == 20 and state["small_blind"] == 10
    assert state["pot"] == 30
    assert state["min_raise_to"] == 40
    assert state["your_stack"] == 500


def test_random_play_conserves_chips_and_terminates():
    import random
    rng = random.Random(2027)
    kinds = ["fold", "check", "call", "raise", "all_in"]
    for h in range(3000):
        n = rng.randint(2, 10)
        ids = [f"b{i}" for i in range(n)]
        stacks = {b: rng.choice([30, 150, 1000, 2500, 5000, 10000]) for b in ids}
        eng = PokerEngine(f"r{h}", ids, dealer_seat=rng.randrange(n),
                          starting_stacks=stacks, seed=h)
        state = eng.start_hand()
        steps = 0
        while state.get("type") == "action_request":
            seat = state["seat_to_act"]
            p = eng.players[seat]
            assert p.is_active
            act = {"action": rng.choice(kinds),
                   "amount": rng.randint(0, 3 * state["min_raise_to"])}
            state = eng.apply_action(seat, act)
            steps += 1
            assert steps < 500
        assert sum(state["final_stacks"].values()) == sum(stacks.values())
        # Every live player who is not all-in matched the final bet level.
        live = [p for p in eng.players if not p.is_folded]
        top = max(p.total_invested for p in live)
        for p in live:
            assert p.is_all_in or p.total_invested == top


def test_player_all_in_from_blind_is_never_asked_to_act():
    # Heads-up, dealer = SB posts 30 all-in; BB already covers: run it out.
    eng = PokerEngine("h", ["A", "B"], dealer_seat=0,
                      starting_stacks={"A": 30, "B": 5000}, seed=8)
    result = eng.start_hand()
    assert result["type"] == "hand_complete"
    assert result["street"] == "river"
    assert sum(result["final_stacks"].values()) == 5030

    # Heads-up, BB posts 30 all-in; SB must still act (call or fold) first.
    eng = PokerEngine("h", ["A", "B"], dealer_seat=0,
                      starting_stacks={"A": 5000, "B": 30}, seed=8)
    state = eng.start_hand()
    assert state["seat_to_act"] == 0 and state["can_raise"] is False
