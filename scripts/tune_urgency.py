"""Validate the group-stage urgency layer (backend/urgency.py) on the played WC
group matches. For each, take the genuine pre-kickoff strength (run before
kickoff), add the drive tilt to the rating diff, and score 1X2 log-loss vs the
real result. Reports the game-2+ subset separately, where urgency actually bites.

NB: until the tournament reaches matchday 2 there are *no* game-2+ played
matches, so the live signal is empty by construction — urgency is grounded in
football game-theory + history, not yet fit to the 2026 sample. This script is
the bench that will judge it as the group stage unfolds.
"""
from __future__ import annotations

import math

from backend import db as dbm
from backend.config import HOST_CITY_COUNTRY, WC_CONFIDENCE, WC_HOST_ELO_BONUS
from backend.match_model import outcome_probs, params
from backend.urgency import DRIVE_SHIFT, apply, match_urgency, standings_before


def main() -> None:
    con = dbm.connect()
    urg = match_urgency(con)
    sb = standings_before(con)
    samples = []                  # (d, sig_h, sig_a, gp, actual, game2plus)
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
             - (WC_HOST_ELO_BONUS if host == a else 0)) * WC_CONFIDENCE
        u = urg.get(n, {})
        sig_h = u.get("sig_home", (0.45, 0.40))
        sig_a = u.get("sig_away", (0.45, 0.40))
        gp = u.get("gp", 0)
        actual = ("home" if m["home_score"] > m["away_score"]
                  else "away" if m["away_score"] > m["home_score"] else "draw")
        g2 = sb[n][0][1] >= 1 or sb[n][1][1] >= 1
        samples.append((d, sig_h, sig_a, gp, actual, g2))
    con.close()

    base_db = params().get("draw_boost", 0.0)

    def loss(scale, subset):
        s = [x for x in samples if (x[5] or not subset)]
        if not s:
            return None, 0
        tot = 0.0
        for d, sh, sa, gp, r, _ in s:
            # scale the drive tilt by `scale` to sweep its strength
            dh, da = sh[0], sa[0]
            d2, gm, lean = apply(d, sh, sa, gp)
            d2 = d + scale * (d2 - d)            # rescale tilt only
            fc = outcome_probs(d2, draw_boost=base_db + lean, goals_mult=gm)
            tot += -math.log(max(1e-9, fc[r]))
        return tot / len(s), len(s)

    print(f"played group matches: {len(samples)} "
          f"(game-2+: {sum(1 for x in samples if x[5])})  "
          f"DRIVE_SHIFT={DRIVE_SHIFT}")
    for label, subset in (("ALL group games", False), ("game-2+ only", True)):
        ll, n = loss(1.0, subset)
        if ll is None:
            print(f"\n  {label}: (no samples yet)")
            continue
        print(f"\n  {label} (n={n}):")
        for scale in (0.0, 0.5, 1.0, 1.5, 2.0):
            ll, _ = loss(scale, subset)
            print(f"    tilt x{scale:.1f}  log-loss={ll:.4f}")


if __name__ == "__main__":
    main()
