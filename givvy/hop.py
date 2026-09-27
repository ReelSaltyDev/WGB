"""The crowd hop (1.2.0 item E): leave a crowded stream right after its draw when a clearly less crowded one is free.
The rules only, no I/O. givvy/camper.py feeds them (its '# ---- crowd hop (item E) ----' section) and writes every
decision to the hop_decisions table.

Why, measured 9/19-9/24 on two accounts: a win is one draw among everyone in the giveaway at the end, and stays whose
first read was over 30 entrants took 26-28% of camped time at about 0.11 pack wins/h, against about 0.63/h for first
reads of 10 or fewer (0.27/h for 11-30). The bot never re-checks a stream that keeps running giveaways: _maybe_move_on
only runs after a quiet spell, and the pack-only upgrade never runs from a pack-only stream. About 14.4 account-hours a
day are movable; checked twice, the gain is +0.2/0.9/2.0 (statistician) or 0/0.6/1.9 (engineer) pack wins a day.

THE RULE (judge), at a draw in the stream we are in, in this order:
  1. cap          at most hop.max_per_hour voluntary moves (crowd hops and pack-only upgrades) in the last 3600 s
  2. not crowded  never out of a stream expecting fewer than hop.min_crowd entrants at the draw
  3. candidates   free (none of our accounts in it), not just left for being quiet, the same kind or better (a
                  pack-only stream only moves to a pack-only one), a proven rhythm (two starts seen), and a giveaway
                  the socket SAW start at most hop.fresh_s before the leave, so the account lands in a running one
  4. margin       it must promise hop.margin x the expected pack wins/h here, both sides on the one crowd model
                  (givvy/crowd.py through chooser.value_of): on the old 2 + 0.18 x viewers every current stream looked
                  2-3x worse than every candidate, and the rule would churn up to the cap
The first candidate in rank order (kind first, then value) that clears the margin is the one.

Socket facts (9/16 socket data): the next giveaway opens a median 14 s after a draw, 27% of pairs in the same frame
(31 s excluding those), so the decision has to come first; giveaways run a median 204-251 s, and about 95% of those
seen starting under 200 s ago are still open 20 s later. Entry to draw: median 275 s, p10 124 s. No win has ever been
drawn to an account that left before the draw (0 of 237 early leaves): it never leaves with an entry still open.

Cost: joins rise from about 2 to at most about 3 per account-hour. More automated movement is more Terms-of-Service
exposure, hence the hard cap and the one switch, hop.mode: live | log | off.

LIVE (Camper._hop_tick, Camper._hop_holds_entry), John's guardrails taken literally:
  1. decide before the next giveaway opens: 27% of draws have it open in the same socket frame (median gap 14 s). So
     the decision is ARMED while our entry is open ('would I leave if the draw happened now?', every 30 s), and an
     armed account enters nothing new in that stream until the draw is judged
  2. stay hop.linger_s (25 s) after the draw, entering nothing: counted from the later of the draw and the decision,
     so a draw signal taken off the queue late still gets linger_s of reads before the leave
  3. never leave while our entry could still be open: a leave needs a draw signal (the socket's 'ended' for our
     giveaway, or a new giveaway proven on screen) AND on-screen proof that ours is gone (no badge in at least two
     reads, or a badge proven to be a giveaway we are not in: an Enter button or the gift icon). 'You're in' or the
     tick at any read after the decision calls it off. The socket alone is never enough: about 5% of its id changes
     came mid-giveaway, and the card flickers constantly (543 holding <-> watching flips in 4.8 h)
  4. at most hop.max_per_hour voluntary moves in any trailing hour, from hop_decisions (a restart does not reset it);
     in live mode a pack-only upgrade waits at the cap too, so the limit holds for every voluntary move. The upgrade
     also never leaves with our entry open (pacing.upgrade_after_draw): live, it is decided at a draw the hop stays for
     and carried out like a hop (HopPlan.action UPGRADE), with the linger, the proof and an 'upgrade' row
  5. never into a stream another of our accounts holds: claimed at the last moment, the claim moving in one step
  6. every judged draw writes exactly one row: stayed, would_leave (log), left, upgrade (the pack-only upgrade decided
     at that draw), or cancelled and why (a pending move is written when it ends)
  7. one switch: hop.mode. 'off' or 'log' during the linger calls the pending move off
Then the rule is judged again at the moment of leaving, with the fresh giveaway measured from then.

Risks, stated for the trial:
  - Terms of Service: joins rise from about 2 to at most about 3 per account-hour, and join, enter, leave at the draw
    is the typical sign of giveaway farming. The cap, the 25 s linger and the switch limit it; mode = 'off' reverts E
    alone without reverting 1.2.0.
  - Leaving 25-30 s after a draw is untested against any 'winner must be present' seller rule. It never leaves before
    the draw is proven by signal and screen, and stays after a win of its own. The 0-of-237 fact covers leaving before
    a draw only.
  - Supply: every gain assumes a better free stream exists at the draw; mornings may be all crowded (the relative rule
    then stays). hop_decisions shows for the first time how often no_candidate happens.
  - Device load: a badge-icon screenshot at most every 5 s, only during a linger or at a suspected new card while
    armed; rank_streams once per draw and at most every 30 s while holding an entry.
  - 'live' is the default, so it reaches the friend's install too if 1.2.0 is published before the trial ends: John's
    call at release time.
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import LiveShow

LIVE, LOG = "live", "log"
ON = (LIVE, LOG)            # the hop.mode values that judge draws; anything else is off

# hop_decisions.action
LEFT, STAYED, WOULD_LEAVE, CANCELLED, UPGRADE = "left", "stayed", "would_leave", "cancelled", "upgrade"
# hop_decisions.reason
BETTER = "better_stream"
NOT_CROWDED = "not_crowded"
NO_CANDIDATE = "no_candidate"
NOT_BETTER = "not_better_enough"
CAP = "cap"
WON = "won_here"
OPEN = "entry_still_open"
UNPROVEN = "draw_not_proven"
CLAIM = "claim_lost"
OFF = "switched_off"
UNSEEN = "draw_not_seen"
PACK_ONLY_FREE = "pack_only_free"
STAY_ENDED = "stay_ended"           # the stay ended some other way during the linger (the stream ended, a pin, Stop...)
# hop_decisions.trigger
SOCKET, SCREEN, TIMEOUT, CHECK = "socket", "screen", "timeout", "check"

# A reason in plain words, for the window ('staying in x: ...')
WORDS = {CAP: "the hourly limit of moves is reached", NOT_CROWDED: "not crowded enough to leave",
         NO_CANDIDATE: "no free stream with a fresh giveaway", NOT_BETTER: "nothing free is clearly better",
         WON: "won here", OPEN: "our entry may still be open", UNPROVEN: "the draw is not proven on screen",
         CLAIM: "another of our accounts took that stream", OFF: "the crowd hop was switched off"}


@dataclass
class Side:
    """One stream as the rule sees it: the one we are in, or a candidate."""
    show: LiveShow                  # the latest discovery entry (current viewers)
    kind: str                       # givvy.chooser kind
    viewers: int
    crowd: float                    # expected final entrants (ActivityBook.entrants)
    per_hour: float                 # giveaways per hour as observed (ActivityBook.rate)
    share: float                    # the share of them that are packs (chooser.pack_share)
    value: float                    # per_hour / crowd x share: expected pack wins per hour
    seen_median: float | None = None   # median first read our accounts took here; None = never read
    age_s: float | None = None      # a candidate: how old its fresh giveaway is at the moment of leaving

    def row(self) -> dict:
        """The Store.record_hop format."""
        return {"show_id": self.show.id, "seller": self.show.seller, "viewers": self.viewers,
                "seen_median": self.seen_median, "crowd": self.crowd, "per_hour": self.per_hour,
                "share": self.share, "value": self.value, "age_s": self.age_s}


@dataclass
class Verdict:
    leave: bool
    reason: str
    cur: Side
    best: Side | None               # leaving: the stream to go to. Staying: the best candidate by value, so the log
                                    # shows the numbers (None = no candidate)
    ok: list[Side]                  # the candidates that clear the margin, in rank order
    moves: int                      # this account's voluntary moves in the hour before


@dataclass
class HopPlan:
    """A leave decided at a draw in live mode, carried out after the linger if the screen proves the draw and the rule,
    judged again then, still says leave. Its row is written when it ends: left (or upgrade), or cancelled and why."""
    target_id: str                  # the stream chosen at the draw (the leave may take the next one in rank order)
    decided_at: float
    drawn_at: float                 # the socket's 'ended', or the read that saw a new giveaway (trigger 'screen')
    trigger: str
    verdict: Verdict                # at the draw: the numbers a cancelled row shows
    action: str = LEFT              # LEFT: a crowd hop; UPGRADE: the pack-only upgrade, decided at a draw the hop stays
                                    # for (Camper._plan_upgrade) and judged again by its own rule at the moment of leaving
    reads: int = 0                  # screen reads in the stream since the decision
    badge_seen: bool = False        # a giveaway badge was on screen since the decision...
    not_ours: bool = False          # ...and was proven a giveaway we are not in: an Enter button, or the gift icon
    tick_at: float = 0.0            # the last badge-icon screenshot


def judge(cur: Side, cands: list[Side], cfg, moves: int) -> Verdict:
    """`cands`: free, fresh candidates in rank order (see the module docstring). `cfg`: HopCfg. `moves`: this account's
    voluntary moves in the last hour."""
    top = max(cands, key=lambda c: c.value) if cands else None
    if moves >= cfg.max_per_hour:
        return Verdict(False, CAP, cur, top, [], moves)
    if cur.crowd < cfg.min_crowd:
        return Verdict(False, NOT_CROWDED, cur, top, [], moves)
    if not cands:
        return Verdict(False, NO_CANDIDATE, cur, None, [], moves)
    # value > 0: never from one stream with no pack to another when the one we are in scores 0
    ok = [c for c in cands if c.value > 0 and c.value >= cfg.margin * cur.value]
    if not ok:
        return Verdict(False, NOT_BETTER, cur, top, [], moves)
    return Verdict(True, BETTER, cur, ok[0], ok, moves)
