"""Group-stage game theory: how each team's *situation* bends a match away from
the neutral Elo+Poisson prediction. Three distinct, bounded signals — this is the
"why the favourite-price is wrong" layer for group games.

The user's framing, made concrete:
  drive     — "how likely a team is *trying to win*": how badly it needs at least
              a draw / a win from this match, read off the table.
  ambition  — "pondered on how much they can win": how badly it needs GOALS
              specifically (chase a goal-difference gap, must win by a margin).
              A team that only needs a point sits; a team that must overturn GD
              throws bodies forward.
  draw_lean — mutual convenience: when *neither* side needs to win (a draw suits
              both — classic final-group-game stalemate), real draw rates spike
              well above the Poisson baseline.

These combine into (tilt, open, draw_lean):
  tilt      = rating points toward the higher-drive team (they press, the other
              manages the game).
  open      = goal multiplier — combined ambition opens the game up (more shots,
              more goals, more variance).
  draw_lean = extra draw inflation added to the score-matrix diagonal.

Everything is group-only and bounded so it can never overturn the proven base
model. Knockouts get nothing (both teams must win — it all cancels). The played
2026 slate is still matchday 1, so this is validated by football logic + history
(see scripts/tune_urgency.py), not yet by the live sample.
"""
from __future__ import annotations

DRIVE_SHIFT = 26.0      # Elo points per unit drive differential (bounded ~±26)
AMBITION_OPEN = 0.16    # how much combined ambition opens the game up
AMBITION_BASE = 0.30    # baseline ambition that already lives in the Poisson fit
DRAW_LEAN_MAX = 0.22    # max extra draw_boost from a mutual-convenience standoff


def team_signals(pts: int, gp: int, gd: int = 0) -> tuple[float, float]:
    """(drive, ambition), each 0..1, from a team's standing *before* the match.

    drive    rises as a team needs a result to stay alive.
    ambition rises when only goals will do — trailing on GD, or must-win-big.
    """
    if gp <= 0:                                   # matchday 1: everyone opens
        return 0.45, 0.40                         # bright, even start
    if gp == 1:                                   # before matchday 2
        drive = {0: 1.00, 1: 0.66, 3: 0.42}.get(pts, 0.55)
        # ambition: a team that lost (0 pts) must start winning *and* fix GD
        ambition = 0.75 if pts == 0 else 0.45 if pts == 1 else 0.35
        if gd <= -2:                              # heavy opening loss → chase goals
            ambition = min(1.0, ambition + 0.20)
        return drive, ambition
    if gp == 2:                                   # before the decider
        if pts <= 1:                              # win-or-out
            return 0.97, 0.85
        if pts == 3:                              # a win clinches, a draw might not
            return 0.74, 0.60
        if pts == 4:                              # likely through, but seeding live
            return 0.46, 0.40
        return 0.28, 0.25                         # 6 pts: through, rotate, coast
    return 0.40, 0.35


def _draw_lean(d_h: float, d_a: float, gp: int) -> float:
    """Both sides content with a point → inflate the draw. Strongest on the final
    matchday, when 'a draw sends us both through' is a live, mutual incentive."""
    calm = (1.0 - d_h) * (1.0 - d_a)              # both low-drive → near 1
    stage = 1.0 if gp == 2 else 0.45 if gp == 1 else 0.15
    return round(DRAW_LEAN_MAX * calm * stage, 4)


def apply(d: float, sig_h: tuple, sig_a: tuple, gp: int = 1
          ) -> tuple[float, float, float]:
    """Return (adjusted rating diff, goals multiplier, extra draw_boost).

    sig_h / sig_a are (drive, ambition) from team_signals. Backwards-friendly:
    plain floats are accepted and read as drive with neutral ambition."""
    dh, ah = sig_h if isinstance(sig_h, tuple) else (sig_h, AMBITION_BASE)
    da, aa = sig_a if isinstance(sig_a, tuple) else (sig_a, AMBITION_BASE)
    d2 = d + DRIVE_SHIFT * (dh - da)
    gm = 1.0 + AMBITION_OPEN * ((ah + aa) / 2 - AMBITION_BASE)
    lean = _draw_lean(dh, da, gp)
    return d2, gm, lean


# ---- standings plumbing -----------------------------------------------------
def standings_before(con) -> dict[int, tuple]:
    """{match_number: ((h_pts, h_gp, h_gd), (a_pts, a_gp, a_gd))} from results
    *before* each group match — for every group fixture, played or not."""
    rows = list(con.execute(
        "SELECT number, group_letter, home_team, away_team, home_score, away_score, "
        "kickoff_utc FROM matches WHERE stage='group' AND home_team IS NOT NULL "
        "AND away_team IS NOT NULL ORDER BY kickoff_utc, number"))
    tally: dict[str, list] = {}                       # team -> [pts, gp, gd]
    out: dict[int, tuple] = {}
    for m in rows:
        h, a = m["home_team"], m["away_team"]
        th = tally.get(h, [0, 0, 0])
        ta = tally.get(a, [0, 0, 0])
        out[m["number"]] = ((th[0], th[1], th[2]), (ta[0], ta[1], ta[2]))
        if m["home_score"] is not None:               # apply result going forward
            hs, as_ = m["home_score"], m["away_score"]
            th[1] += 1; ta[1] += 1
            th[2] += hs - as_; ta[2] += as_ - hs
            if hs > as_:
                th[0] += 3
            elif as_ > hs:
                ta[0] += 3
            else:
                th[0] += 1; ta[0] += 1
            tally[h], tally[a] = th, ta
    return out


def match_urgency(con) -> dict[int, dict]:
    """{match_number: {sig_home, sig_away, gp}} for group matches — ready to feed
    straight into apply()."""
    sb = standings_before(con)
    out = {}
    for n, ((hp, hg, hgd), (ap, ag, agd)) in sb.items():
        out[n] = {"sig_home": team_signals(hp, hg, hgd),
                  "sig_away": team_signals(ap, ag, agd),
                  "gp": max(hg, ag)}
    return out
