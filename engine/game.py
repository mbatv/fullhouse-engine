"""
Fullhouse Hackathon — No-Limit Texas Hold'em Game Engine v2.0
6-max (up to 9), using eval7 (same library as MIT Pokerbots).

Fixes in v2.0:
  - Correct side-pot computation (multiple all-in levels)
  - Heads-up rules: dealer = SB, SB acts first preflop, BB first postflop
  - BB option: BB is included in needs_to_act preflop
  - Short all-in: does NOT reopen action for players who already acted
  - Accurate blind posting: logs real contributed amounts
  - Deterministic/seeded deck for reproducible matches
  - Explicit player states: active / folded / all_in / busted
  - Chip invariant check after every hand resolution
  - Rich event log for full replay (street_start, blind, action, showdown)
  - Hand strength labels at showdown
"""

import eval7
import random
from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Defaults (the 2026 qualifier settings). PokerEngine takes per-table
# overrides so 2027 tracks can run other blind levels and stack depths.
SMALL_BLIND    = 50
BIG_BLIND      = 100
STARTING_STACK = 10_000
MAX_PLAYERS    = 10


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Player:
    seat: int
    bot_id: str
    stack: int
    hole_cards: list = field(default_factory=list)
    is_folded: bool = False
    is_all_in: bool = False
    bet_this_street: int = 0
    total_invested: int = 0     # cumulative chips put in this hand (for side pots)

    @property
    def is_active(self) -> bool:
        """Can still make betting decisions."""
        return not self.is_folded and not self.is_all_in and self.stack > 0

    @property
    def state(self) -> str:
        if self.is_folded:
            return "folded"
        if self.is_all_in:
            return "all_in"
        if self.stack == 0:
            return "busted"
        return "active"

    def to_public_dict(self) -> dict:
        return {
            "seat": self.seat,
            "bot_id": self.bot_id,
            "stack": self.stack,
            "state": self.state,
            "is_folded": self.is_folded,
            "is_all_in": self.is_all_in,
            "bet_this_street": self.bet_this_street,
            "hole_cards": None,      # hidden until showdown
        }


@dataclass
class Action:
    seat: int
    action: str
    amount: int = 0

    def to_dict(self) -> dict:
        return {"seat": self.seat, "action": self.action, "amount": self.amount}


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class PokerEngine:
    def __init__(
        self,
        hand_id: str,
        bot_ids: list,
        dealer_seat: int = 0,
        starting_stacks: Optional[dict] = None,
        seed: Optional[int] = None,
        small_blind: int = SMALL_BLIND,
        big_blind: int = BIG_BLIND,
        starting_stack: int = STARTING_STACK,
    ):
        assert 2 <= len(bot_ids) <= MAX_PLAYERS, \
            f"Need 2-{MAX_PLAYERS} bots, got {len(bot_ids)}"

        assert 0 < small_blind <= big_blind, "Need 0 < small_blind <= big_blind"
        self.small_blind = small_blind
        self.big_blind   = big_blind

        stacks = starting_stacks or {}
        self.players = [
            Player(seat=i, bot_id=bid, stack=stacks.get(bid, starting_stack))
            for i, bid in enumerate(bot_ids)
        ]
        self.n           = len(self.players)
        self.dealer_seat = dealer_seat % self.n
        self.hand_id     = hand_id
        self.seed        = seed

        self.pot             = 0
        self.community_cards = []          # list of eval7.Card
        self.street          = "preflop"
        self.action_log      = []          # flat dicts (backwards-compat for bots)
        self.events          = []          # rich event log for replay
        self.current_bet     = 0
        self.min_raise       = big_blind

        # Short all-in: only reopen action if raise >= last full raise size
        self._last_aggression_size = big_blind
        # current_bet at the moment each seat last acted this street. A seat
        # may raise only if it has not acted yet, or if the bet has grown by
        # at least one full raise since it acted (so stacked short all-ins
        # that add up to a full raise reopen the betting).
        self._acted_at_bet = {}

        self._needs_to_act    = set()
        self._deck_cards      = []         # list[eval7.Card] after shuffle
        self._deck_idx        = 0
        self._starting_stacks = {}         # snapshot before hand starts

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def start_hand(self) -> dict:
        self._snapshot_stacks()
        self._build_deck()
        self._post_blinds()
        self._deal_hole_cards()

        # Everyone must act preflop, including BB (BB gets the option)
        self._needs_to_act = {p.seat for p in self.players if p.is_active}

        self._emit("street_start", {"street": "preflop", "community_cards": []})

        # Blinds can put players all-in. Nobody is asked to act unless they
        # can still make a decision: with at most one active player who
        # already matches the bet, the board is simply run out.
        active = [p for p in self.players if p.is_active]
        if not active or (len(active) == 1
                          and active[0].bet_this_street >= self.current_bet):
            return self._run_it_out()
        first = self._utg_seat()
        if not self.players[first].is_active:
            first = self._next_actor(first)
        return self._build_state(first)

    def apply_action(self, seat: int, raw: dict) -> dict:
        action = self._validate(seat, raw)
        self.action_log.append(action.to_dict())
        self._needs_to_act.discard(seat)
        p = self.players[seat]
        acted_at_bet = self.current_bet

        if action.action == "fold":
            p.is_folded = True
            self._emit_action(seat, "fold", 0)

        elif action.action == "check":
            self._emit_action(seat, "check", 0)

        elif action.action == "call":
            owed = self.current_bet - p.bet_this_street
            paid = min(owed, p.stack)
            self._put_in(seat, paid)
            if p.stack == 0:
                p.is_all_in = True
            self._emit_action(seat, "call", paid)

        elif action.action == "raise":
            prev_bet   = self.current_bet
            chips_in   = action.amount - p.bet_this_street
            self._put_in(seat, min(chips_in, p.stack))
            if p.stack == 0:
                p.is_all_in = True
            raise_size = self.current_bet - prev_bet
            self._handle_aggression(seat, raise_size)
            self._emit_action(seat, "raise", action.amount)

        elif action.action == "all_in":
            prev_bet = self.current_bet
            self._put_in(seat, p.stack)
            p.is_all_in = True
            raise_size = self.current_bet - prev_bet
            self._handle_aggression(seat, raise_size)
            self._emit_action(seat, "all_in", p.bet_this_street)

        self._acted_at_bet[seat] = max(acted_at_bet, p.bet_this_street)

        # Everyone folded except one?
        remaining = [pl for pl in self.players if not pl.is_folded]
        if len(remaining) == 1:
            return self._award_uncontested(remaining[0])

        return self._advance_if_street_over(seat)

    # -----------------------------------------------------------------------
    # Heads-up seat helpers
    # -----------------------------------------------------------------------

    @property
    def _is_heads_up(self) -> bool:
        return self.n == 2

    def _sb_seat(self) -> int:
        """Heads-up: dealer IS the small blind."""
        if self._is_heads_up:
            return self.dealer_seat
        return (self.dealer_seat + 1) % self.n

    def _bb_seat(self) -> int:
        if self._is_heads_up:
            return (self.dealer_seat + 1) % self.n
        return (self.dealer_seat + 2) % self.n

    def _utg_seat(self) -> int:
        """
        Preflop first actor.
        Heads-up: SB (dealer) acts first.
        Normal:   first active seat after BB.
        """
        if self._is_heads_up:
            return self._sb_seat()
        bb = self._bb_seat()
        for offset in range(1, self.n + 1):
            s = (bb + offset) % self.n
            if self.players[s].is_active:
                return s
        return bb  # fallback

    def _first_postflop_actor(self) -> Optional[int]:
        """
        Postflop: first active player left of dealer.
        Heads-up: BB (non-dealer) acts first postflop.
        """
        if self._is_heads_up:
            for seat in [self._bb_seat(), self._sb_seat()]:
                if self.players[seat].is_active:
                    return seat
            return None
        for offset in range(1, self.n + 1):
            s = (self.dealer_seat + offset) % self.n
            if self.players[s].is_active:
                return s
        return None

    # -----------------------------------------------------------------------
    # Action flow
    # -----------------------------------------------------------------------

    def _handle_aggression(self, seat: int, raise_size: int):
        """
        If raise_size >= last full raise: reopen action for everyone except aggressor.
        If short all-in (raise_size < last full raise): everyone who now owes
        chips must still call or fold, but seats that already acted may not
        re-raise unless the bet has grown by a full raise since they acted
        (see _can_raise).
        """
        if raise_size >= self._last_aggression_size:
            self._last_aggression_size = raise_size
            self.min_raise = raise_size
            self._needs_to_act = {
                p.seat for p in self.players
                if p.is_active and p.seat != seat
            }
        elif raise_size > 0:
            self._needs_to_act |= {
                p.seat for p in self.players
                if p.is_active and p.seat != seat
                and p.bet_this_street < self.current_bet
            }

    def _can_raise(self, seat: int) -> bool:
        p = self.players[seat]
        if p.stack <= self.current_bet - p.bet_this_street:
            return False        # can only call (possibly all-in) or fold
        if not any(o.is_active for o in self.players if o.seat != seat):
            return False        # nobody left who could call a raise
        if seat not in self._acted_at_bet:
            return True
        return self.current_bet - self._acted_at_bet[seat] >= self._last_aggression_size

    def _next_actor(self, from_seat: int) -> Optional[int]:
        if not self._needs_to_act:
            return None
        for offset in range(1, self.n + 1):
            s = (from_seat + offset) % self.n
            if s in self._needs_to_act and self.players[s].is_active:
                return s
        return None

    def _advance_if_street_over(self, last_seat: int) -> dict:
        nxt = self._next_actor(last_seat)
        if nxt is not None:
            return self._build_state(nxt)
        return self._advance_street()

    def _advance_street(self) -> dict:
        for p in self.players:
            p.bet_this_street = 0
        self.current_bet           = 0
        self.min_raise             = self.big_blind
        self._last_aggression_size = self.big_blind
        self._acted_at_bet         = {}

        if self.street == "preflop":
            self.community_cards += self._deal(3)
            self.street = "flop"
        elif self.street == "flop":
            self.community_cards += self._deal(1)
            self.street = "turn"
        elif self.street == "turn":
            self.community_cards += self._deal(1)
            self.street = "river"
        elif self.street == "river":
            return self._showdown()

        self._emit("street_start", {
            "street":          self.street,
            "community_cards": [str(c) for c in self.community_cards],
        })

        first = self._first_postflop_actor()
        if first is None or sum(p.is_active for p in self.players) < 2:
            return self._run_it_out()

        self._needs_to_act = {p.seat for p in self.players if p.is_active}
        return self._build_state(first)

    def _run_it_out(self) -> dict:
        """All remaining players are all-in — run out the board silently."""
        if self.street == "preflop":
            self.community_cards += self._deal(3)
            self.street = "flop"
        if self.street == "flop":
            self.community_cards += self._deal(1)
            self.street = "turn"
        if self.street == "turn":
            self.community_cards += self._deal(1)
            self.street = "river"
        return self._showdown()

    # -----------------------------------------------------------------------
    # Chips
    # -----------------------------------------------------------------------

    def _snapshot_stacks(self):
        self._starting_stacks = {p.bot_id: p.stack for p in self.players}

    def _post_blinds(self):
        sb, bb       = self._sb_seat(), self._bb_seat()
        sb_amount    = min(self.small_blind, self.players[sb].stack)
        bb_amount    = min(self.big_blind,   self.players[bb].stack)
        self._put_in(sb, sb_amount)
        self._put_in(bb, bb_amount)
        self.current_bet           = max(self.current_bet, self.big_blind)
        self.min_raise             = self.big_blind
        self._last_aggression_size = self.big_blind
        if self.players[sb].stack == 0:
            self.players[sb].is_all_in = True
        if self.players[bb].stack == 0:
            self.players[bb].is_all_in = True
        self.action_log.append({"seat": sb, "action": "small_blind", "amount": sb_amount})
        self.action_log.append({"seat": bb, "action": "big_blind",   "amount": bb_amount})
        self._emit("blind", {"seat": sb, "bot_id": self.players[sb].bot_id,
                             "action": "small_blind", "amount": sb_amount})
        self._emit("blind", {"seat": bb, "bot_id": self.players[bb].bot_id,
                             "action": "big_blind",   "amount": bb_amount})

    def _put_in(self, seat: int, amount: int):
        amount = max(0, min(amount, self.players[seat].stack))
        self.players[seat].stack           -= amount
        self.players[seat].bet_this_street += amount
        self.players[seat].total_invested  += amount
        self.pot                           += amount
        if self.players[seat].bet_this_street > self.current_bet:
            self.current_bet = self.players[seat].bet_this_street

    # -----------------------------------------------------------------------
    # Deck
    # -----------------------------------------------------------------------

    def _build_deck(self):
        """Build and (optionally deterministic) shuffle a full 52-card deck."""
        ranks = "23456789TJQKA"
        suits = "shdc"
        cards = [eval7.Card(r + s) for r in ranks for s in suits]
        if self.seed is not None:
            rng = random.Random(self.seed)
            rng.shuffle(cards)
        else:
            random.shuffle(cards)
        self._deck_cards = cards
        self._deck_idx   = 0

    def _deal(self, n: int) -> list:
        cards          = self._deck_cards[self._deck_idx: self._deck_idx + n]
        self._deck_idx += n
        return cards

    def _deal_hole_cards(self):
        for p in self.players:
            p.hole_cards = self._deal(2)

    # -----------------------------------------------------------------------
    # Validation
    # -----------------------------------------------------------------------

    def _validate(self, seat: int, raw: dict) -> Action:
        p   = self.players[seat]
        act = str(raw.get("action", "fold")).lower().strip()
        try:
            amount = int(raw.get("amount") or 0)
        except (TypeError, ValueError):
            amount = 0

        if act not in ("fold", "check", "call", "raise", "all_in"):
            return Action(seat, "fold")

        owed = self.current_bet - p.bet_this_street

        if act == "check":
            return Action(seat, "check") if owed == 0 else Action(seat, "call", owed)

        if act == "call":
            return Action(seat, "check") if owed == 0 else Action(seat, "call", owed)

        if act in ("raise", "all_in") and not self._can_raise(seat):
            if owed == 0:
                return Action(seat, "check")
            if p.stack <= owed:
                return Action(seat, "all_in", p.stack + p.bet_this_street)
            return Action(seat, "call", owed)

        if act == "raise":
            min_total    = self.current_bet + self.min_raise
            amount       = max(amount, min_total)
            chips_needed = amount - p.bet_this_street
            if chips_needed >= p.stack:
                return Action(seat, "all_in", p.stack + p.bet_this_street)
            return Action(seat, "raise", amount)

        if act == "all_in":
            return Action(seat, "all_in", p.stack + p.bet_this_street)

        return Action(seat, act, amount)

    # -----------------------------------------------------------------------
    # Resolution
    # -----------------------------------------------------------------------

    def _showdown(self) -> dict:
        contenders = [p for p in self.players if not p.is_folded]
        if len(contenders) == 1:
            return self._award_uncontested(contenders[0])

        scored = [
            (eval7.evaluate(p.hole_cards + self.community_cards), p)
            for p in contenders
        ]

        # Hand strength labels
        hand_strengths = {}
        for score, p in scored:
            try:
                hand_strengths[p.bot_id] = str(eval7.handtype(score))
            except Exception:
                hand_strengths[p.bot_id] = "unknown"

        # Side-pot resolution
        side_pots   = self._compute_side_pots()
        winners_log = []

        for pot_info in side_pots:
            eligible_ids    = {p.bot_id for p in pot_info["eligible"]}
            eligible_scored = [(s, p) for s, p in scored if p.bot_id in eligible_ids]
            best            = max(s for s, _ in eligible_scored)
            # Odd chips go to the first winner left of the button.
            pot_winners     = sorted(
                (p for s, p in eligible_scored if s == best),
                key=lambda p: (p.seat - self.dealer_seat - 1) % self.n,
            )

            split     = pot_info["amount"] // len(pot_winners)
            remainder = pot_info["amount"] % len(pot_winners)
            for i, w in enumerate(pot_winners):
                award = split + (remainder if i == 0 else 0)
                w.stack += award
                winners_log.append({
                    "bot_id":   w.bot_id,
                    "seat":     w.seat,
                    "amount":   award,
                    "pot_type": "main" if pot_info is side_pots[0] else "side",
                })

        revealed = {p.bot_id: [str(c) for c in p.hole_cards] for _, p in scored}

        self._emit("showdown", {
            "community_cards": [str(c) for c in self.community_cards],
            "revealed":        revealed,
            "hand_strengths":  hand_strengths,
            "winners":         winners_log,
        })

        self._check_invariants()
        return self._build_result(winners_log, showdown=True,
                                  revealed=revealed, hand_strengths=hand_strengths)

    def _award_uncontested(self, winner: Player) -> dict:
        winner.stack += self.pot
        result = [{"bot_id": winner.bot_id, "seat": winner.seat,
                   "amount": self.pot, "pot_type": "main"}]
        self._emit("uncontested_win", {"bot_id": winner.bot_id, "amount": self.pot})
        self._check_invariants()
        return self._build_result(result, showdown=False)

    def _compute_side_pots(self) -> list:
        """
        Build side pots based on total_invested per player.
        Returns list of {amount, eligible} sorted smallest to largest.
        """
        in_players = [p for p in self.players if not p.is_folded]
        if not in_players:
            return [{"amount": self.pot, "eligible": []}]

        levels   = sorted(set(p.total_invested for p in self.players
                               if p.total_invested > 0))
        pots     = []
        prev_lvl = 0

        for lvl in levels:
            per_player   = lvl - prev_lvl
            contributors = [p for p in self.players if p.total_invested >= lvl]
            pot_amount   = per_player * len(contributors)
            eligible     = [p for p in in_players if p.total_invested >= lvl]
            if pot_amount > 0 and eligible:
                pots.append({"amount": pot_amount, "eligible": eligible})
            prev_lvl = lvl

        if not pots:
            return [{"amount": self.pot, "eligible": in_players}]

        # Absorb any rounding residual
        total = sum(p["amount"] for p in pots)
        if total != self.pot:
            pots[-1]["amount"] += self.pot - total

        return pots

    # -----------------------------------------------------------------------
    # Invariants
    # -----------------------------------------------------------------------

    def _check_invariants(self):
        total_start = sum(self._starting_stacks.values())
        total_now   = sum(p.stack for p in self.players)
        if total_now != total_start:
            raise AssertionError(
                f"[{self.hand_id}] Chip invariant violated: "
                f"started={total_start}, now={total_now}. "
                f"Stacks: {[(p.bot_id, p.stack) for p in self.players]}"
            )

    # -----------------------------------------------------------------------
    # Events
    # -----------------------------------------------------------------------

    def _emit(self, event_type: str, data: dict):
        self.events.append({
            "type":   event_type,
            "street": self.street,
            "pot":    self.pot,
            **data,
        })

    def _emit_action(self, seat: int, action: str, amount: int):
        p = self.players[seat]
        self._emit("action", {
            "seat":        seat,
            "bot_id":      p.bot_id,
            "action":      action,
            "amount":      amount,
            "pot_after":   self.pot,
            "stack_after": p.stack,
            "stacks":      {pl.bot_id: pl.stack for pl in self.players},
        })

    # -----------------------------------------------------------------------
    # Serialisation
    # -----------------------------------------------------------------------

    def _build_state(self, seat: int) -> dict:
        p    = self.players[seat]
        owed = max(0, self.current_bet - p.bet_this_street)
        return {
            "type":                  "action_request",
            "hand_id":               self.hand_id,
            "street":                self.street,
            "seat_to_act":           seat,
            "pot":                   self.pot,
            "community_cards":       [str(c) for c in self.community_cards],
            "current_bet":           self.current_bet,
            "min_raise_to":          self.current_bet + self.min_raise,
            "amount_owed":           owed,
            "can_check":             owed == 0,
            "can_raise":             self._can_raise(seat),
            "small_blind":           self.small_blind,
            "big_blind":             self.big_blind,
            "your_cards":            [str(c) for c in p.hole_cards],
            "your_stack":            p.stack,
            "your_bet_this_street":  p.bet_this_street,
            "players":               [pl.to_public_dict() for pl in self.players],
            "action_log":            list(self.action_log),
        }

    def _build_result(
        self,
        winners: list,
        showdown: bool,
        revealed: Optional[dict] = None,
        hand_strengths: Optional[dict] = None,
    ) -> dict:
        return {
            "type":            "hand_complete",
            "hand_id":         self.hand_id,
            "street":          self.street,
            "pot":             self.pot,
            "community_cards": [str(c) for c in self.community_cards],
            "winners":         winners,
            "showdown":        showdown,
            "revealed_cards":  revealed or {},
            "hand_strengths":  hand_strengths or {},
            "action_log":      list(self.action_log),
            "events":          list(self.events),
            "final_stacks":    {p.bot_id: p.stack for p in self.players},
        }
