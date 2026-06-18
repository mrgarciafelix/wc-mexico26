"""Test whether group-stage urgency improves predictions on the played WC group
matches. For each, we take the model's genuine pre-kickoff strength (from the run
before kickoff), add SHIFT * (urgency_home - urgency_away) to the rating diff, and
score the 1X2 log-loss vs the real result. Progressive: also reports on only the
game-2+ subset where urgency actually differs.
"""
from __future__ import annotations

import math

from backend import db as dbm
from backend.config import HOST_CITY_COUNTRY, WC_HOST_ELO_BONUS
from backend.match_model import outcome_probs
from backend.urgency import match_urgency, standings_before


def main() -> None:
    con = dbm.connect()
    urg = match_urgency(con)
    sb = standings_before(con)
    samples = []                  # (d_eff, u_diff, actual, game2plus)
    for m in con.execute(
            "SELECT * FROM matches WHERE stage='group' AND home_score IS NOT NULL "
            "AND home_team IS NOT NULL ORDER BY kickoff_utc"):
        n = m["number"]
        pre = con.execute("SELECT id FROM runs WHERE ts < ? ORDER BY ts DESC LIMIT 1",
                          (m["kickoff_utc"],)).fetchone()
        if not pre:
            continue
        st = {r["team"]: r["strength"] for r in con.execute(
            "SELECT team, strength FROM strengths WHERE run_id=?", (pre["id"],))}
        h, a = m["home_team"], m["away_team"]
        if h not in st or a not in st:
            continue
        host = HOST_CITY_COUNTRY.get(m["city"])
        d = (st[h] - st[a] + (WC_HOST_ELO_BONUS if host == h else 0)
             - (WC_HOST_ELO_BONUS if host == a else 0))
        uh, ua = urg.get(n, (0.35, 0.35))
        actual = ("home" if m["home_score"] > m["away_score"]
                  else "away" if m["away_score"] > m["home_score"] else "draw")
        g2 = sb[n][0][1] >= 1 or sb[n][1][1] >= 1
        samples.append((d, uh - ua, actual, g2))
    con.close()

    def loss(shift, subset):
        s = [x for x in samples if (x[3] or not subset)]
        if not s:
            return None, 0
        ll = sum(-math.log(max(1e-9, outcome_probs(d + shift * ud)[r]))
                 for d, ud, r, _ in s) / len(s)
        return ll, len(s)

    print(f"played group matches: {len(samples)} "
          f"(game-2+: {sum(1 for x in samples if x[3])})")
    for label, subset in (("ALL group games", False), ("game-2+ only", True)):
        print(f"\n  {label}:")
        for shift in [0, 20, 40, 60, 80, 100, 140]:
            ll, n = loss(shift, subset)
            if ll is not None:
                print(f"    shift={shift:4d}  log-loss={ll:.4f}  (n={n})")


if __name__ == "__main__":
    main()
