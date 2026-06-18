"""Group-stage 'urgency': how badly each team needs a win, from their current
group standing. A team that lost/drew its opener is desperate in game 2; a team
already through is relaxed. The *differential* nudges the match toward the more
desperate side and opens the game up (more goals/shots/corners). Knockouts get no
urgency-diff (both equally need to win — it cancels). Bounded and group-only, so
it can never break the proven Elo+Poisson base.
"""
from __future__ import annotations

URGENCY_SHIFT = 30.0     # Elo points per unit urgency differential (bounded ~±28)
URGENCY_GOALS = 0.15     # how much combined urgency opens the game up
URGENCY_BASE = 0.35      # game-1 baseline (no effect when both teams sit here)


def apply(d: float, u_h: float, u_a: float) -> tuple[float, float]:
    """Return (adjusted rating diff, goals multiplier) for a group match."""
    d2 = d + URGENCY_SHIFT * (u_h - u_a)
    gm = 1.0 + URGENCY_GOALS * ((u_h + u_a) / 2 - URGENCY_BASE)
    return d2, gm


def team_urgency(pts: int, gp: int) -> float:
    """0..1, higher = needs a win more."""
    if gp <= 0:
        return 0.35                       # game 1: everyone wants a good start
    if gp == 1:
        return {0: 1.00, 1: 0.68, 3: 0.42}.get(pts, 0.55)
    if gp == 2:                           # before the decider
        if pts <= 1:
            return 0.95                   # win-or-out
        if pts == 3:
            return 0.72
        if pts == 4:
            return 0.45
        return 0.30                       # 6 pts, already through
    return 0.4


def standings_before(con) -> dict[int, tuple]:
    """{match_number: ((home_pts, home_gp), (away_pts, away_gp))} computed from
    results *before* that match — for every group match, played or not."""
    rows = list(con.execute(
        "SELECT number, group_letter, home_team, away_team, home_score, away_score, "
        "kickoff_utc FROM matches WHERE stage='group' AND home_team IS NOT NULL "
        "AND away_team IS NOT NULL ORDER BY kickoff_utc, number"))
    tally: dict[str, list] = {}                       # team -> [pts, gp]
    out: dict[int, tuple] = {}
    for m in rows:
        h, a = m["home_team"], m["away_team"]
        th = tally.get(h, [0, 0])
        ta = tally.get(a, [0, 0])
        out[m["number"]] = ((th[0], th[1]), (ta[0], ta[1]))
        if m["home_score"] is not None:               # apply result going forward
            hs, as_ = m["home_score"], m["away_score"]
            th[1] += 1
            ta[1] += 1
            if hs > as_:
                th[0] += 3
            elif as_ > hs:
                ta[0] += 3
            else:
                th[0] += 1
                ta[0] += 1
            tally[h], tally[a] = th, ta
    return out


def match_urgency(con) -> dict[int, tuple]:
    """{match_number: (u_home, u_away)} for group matches."""
    sb = standings_before(con)
    return {n: (team_urgency(hp, hg), team_urgency(ap, ag))
            for n, ((hp, hg), (ap, ag)) in sb.items()}
