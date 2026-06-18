"""Backtest the Elo+Poisson match engine on historical internationals.

For every match in a held-out window we predict 1X2 from *pre-match* Elo, then
score it against the real result. This is the honest baseline: it says how good
the model actually is today, and every new feature (form, style, lineups) has to
beat these numbers out-of-sample or it doesn't ship.

Metrics:
  log-loss   lower is better; compared to the no-skill base-rate floor (entropy)
  Brier      multiclass mean squared error of the probabilities
  accuracy   model's top pick vs always-pick-the-commonest baseline
  ECE        calibration error — do "60%" predictions happen ~60% of the time
"""
from __future__ import annotations

import csv
import math

from .config import ELO_HOME_ADV, canonical
from .elo import RESULTS_CSV, EloState, ensure_results_csv
from .match_model import outcome_probs


def _result(h: int, a: int) -> str:
    return "home" if h > a else "away" if a > h else "draw"


def backtest(test_from: str = "2022-01-01", test_to: str = "2026-06-01") -> dict:
    ensure_results_csv()
    state = EloState()
    preds: list[tuple[float, float, float, str]] = []
    counts = {"home": 0, "draw": 0, "away": 0}
    with RESULTS_CSV.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            hs, as_ = row["home_score"], row["away_score"]
            if hs in ("NA", "") or as_ in ("NA", ""):
                continue
            home, away = canonical(row["home_team"]), canonical(row["away_team"])
            neutral = row["neutral"].upper() == "TRUE"
            h, a = int(float(hs)), int(float(as_))
            if test_from <= row["date"] < test_to:
                d = (state.rating[home] + (0 if neutral else ELO_HOME_ADV)
                     - state.rating[away])
                fc = outcome_probs(d)
                act = _result(h, a)
                preds.append((fc["home"], fc["draw"], fc["away"], act))
                counts[act] += 1
            state.apply(home, away, h, a, row["tournament"], neutral)

    n = len(preds)
    if not n:
        return {"n": 0}
    base = {k: counts[k] / n for k in counts}

    ll = sum(-math.log(max(1e-9, {"home": ph, "draw": pd, "away": pa}[act]))
             for ph, pd, pa, act in preds) / n
    base_ll = -sum(base[k] * math.log(base[k]) for k in base if base[k] > 0)
    brier = sum((ph - (act == "home")) ** 2 + (pd - (act == "draw")) ** 2
                + (pa - (act == "away")) ** 2 for ph, pd, pa, act in preds) / n
    hits = sum(max((("home", ph), ("draw", pd), ("away", pa)),
                   key=lambda x: x[1])[0] == act for ph, pd, pa, act in preds)

    bins = [[] for _ in range(10)]
    for ph, pd, pa, act in preds:
        bins[min(9, int(ph * 10))].append((ph, 1.0 if act == "home" else 0.0))
    ece, cal = 0.0, []
    for b in bins:
        if not b:
            continue
        ap = sum(x[0] for x in b) / len(b)
        ao = sum(x[1] for x in b) / len(b)
        ece += abs(ap - ao) * len(b) / n
        cal.append({"pred": round(ap, 2), "actual": round(ao, 2), "n": len(b)})

    return {
        "window": f"{test_from}..{test_to}", "n": n,
        "model_logloss": round(ll, 4), "baseline_logloss": round(base_ll, 4),
        "logloss_edge_pct": round((base_ll - ll) / base_ll * 100, 1),
        "model_brier": round(brier, 4),
        "accuracy": round(hits / n, 4),
        "baseline_accuracy": round(max(base.values()), 4),
        "ece": round(ece, 4), "base_rates": {k: round(v, 3) for k, v in base.items()},
        "home_win_calibration": cal,
    }


def cached(max_age_days: float = 7.0) -> dict:
    """Backtest is slow (replays ~49k matches) and only changes with the model,
    so cache it to data/backtest.json and recompute at most weekly."""
    import json
    import time

    from .config import DATA
    path = DATA / "backtest.json"
    if path.exists():
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
            if (time.time() - d.get("_ts", 0)) / 86400 < max_age_days:
                return d
        except Exception:
            pass
    d = backtest()
    d["_ts"] = time.time()
    try:
        path.write_text(json.dumps(d), encoding="utf-8")
    except Exception:
        pass
    return d


if __name__ == "__main__":
    import json
    print(json.dumps(backtest(), indent=2))
