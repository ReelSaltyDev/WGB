"""How many people will be in a giveaway when it is drawn. ONE calibrated model, used everywhere.

A win is one draw among everyone in the giveaway at the end, so this crowd decides almost everything. Fitted
2026-09-24 on 3,455 confirmed entries from 9/17 to 9/24, against the Givvy Wins tracker (125 wins):

  from viewers        final entrants ~ 7 + 0.67 x viewers                          AIC 997.4; it predicted 96.3 wins
                                                                                   against 96 actual (in-sample)
  from the count      final entrants ~ 4.1 x (seen + 1)                            AIC 1047.3; k 95% range 3.50-4.84
  both (best fit)     final entrants ~ 2.0 + 0.90 x (seen + 1) + 0.54 x viewers    AIC 976.1

'seen' is the entrant count the bot read on the card as it entered. The final crowd is 3.5-4.8x that, because
people keep entering after we do. The chooser's old guess, 2 + 0.18 x viewers, was 3.50x (0 viewers) to 3.72x
(300 viewers) too low, in the same order: its ranking was roughly right, every number ~3.6x too optimistic.

Who uses what:
  the stream chooser and the crowd hop    final_entrants, through ActivityBook.entrants and chooser.value_of
                                          (scoring.crowd_from_seen says whether counts read here refine it)
  the trial report                        from_viewers and from_seen directly, per entry, so its yardstick is
                                          computed the same way in the baseline and the trial whatever the settings
"""
import statistics


def from_viewers(viewers) -> float:
    return 7.0 + 0.67 * max(0, viewers or 0)


def from_seen(seen) -> float:
    return 4.1 * (max(0, seen or 0) + 1)


def final_entrants(viewers, seen=None) -> float:
    """`seen`: the counts read on cards in this stream (the first read at each entry), or None."""
    v = max(0, viewers or 0)
    if seen:
        return 2.0 + 0.90 * (statistics.median(seen) + 1) + 0.54 * v
    return from_viewers(v)
