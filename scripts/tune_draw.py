"""Sweep the draw-boost parameter against the 4,448-game backtest and report the
log-loss for each. We only adopt a value if it lowers log-loss out-of-sample —
the model's calibration discipline, not a fit to the noisy WC sample.

    .venv\\Scripts\\python.exe -m scripts.tune_draw
"""
from __future__ import annotations

import csv
import math

from backend.config import ELO_HOME_ADV, canonical
from backend.elo import RESULTS_CSV, EloState, ensure_results_csv
from backend.match_model import outcome_probs


def main() -> None:
    ensure_results_csv()
    state = EloState()
    samples = []                       # (d_eff, actual) for the test window
    draws = 0
    with RESULTS_CSV.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            hs, as_ = row["home_score"], row["away_score"]
            if hs in ("NA", "") or as_ in ("NA", ""):
                continue
            h, a = canonical(row["home_team"]), canonical(row["away_team"])
            nt = row["neutral"].upper() == "TRUE"
            H, A = int(float(hs)), int(float(as_))
            if "2022-01-01" <= row["date"] < "2026-06-01":
                d = state.rating[h] + (0 if nt else ELO_HOME_ADV) - state.rating[a]
                r = "home" if H > A else "away" if A > H else "draw"
                samples.append((d, r))
                draws += r == "draw"
            state.apply(h, a, H, A, row["tournament"], nt)

    n = len(samples)
    print(f"n={n}  actual draw rate={draws/n:.3f}\n  boost   log-loss   acc   mean P(draw)")
    best = (None, 9e9)
    for db in [0.0, 0.05, 0.08, 0.10, 0.12, 0.15, 0.20, 0.25]:
        ll = hits = mpd = 0.0
        for d, r in samples:
            fc = outcome_probs(d, draw_boost=db)
            ll += -math.log(max(1e-9, fc[r]))
            mpd += fc["draw"]
            pick = max(("home", "draw", "away"), key=lambda k: fc[k])
            hits += pick == r
        ll /= n
        mark = ""
        if ll < best[1]:
            best = (db, ll)
        print(f"  {db:.2f}    {ll:.4f}    {hits/n:.3f}   {mpd/n:.3f}")
    print(f"\nbest draw_boost = {best[0]} (log-loss {best[1]:.4f})")


if __name__ == "__main__":
    main()
