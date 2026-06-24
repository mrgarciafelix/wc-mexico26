"""WORLD-CUP hold-out evaluation — the test the model is actually judged on.

The general bench (scripts/evaluate.py) scores 4,448 games in a 2022-2026-06-01
window, but that window **stops before the tournament kicked off** — it never
sees a single real WC2026 match. This harness closes that gap. It scores the
match engine on the three samples that matter for *this* tournament:

  hist_wc   every historical World Cup match (martj42), pre-match Elo -> 1X2.
            The model's weakest category (cagey, neutral, elite, high-stakes).
  live_wc   the WC2026 matches already played, scored from the genuine
            pre-kickoff strength snapshot in the DB (never the result).
  friendly  the immediate pre-WC friendlies (tune-up window) — fresh, unseen.

It then sweeps the two WC-context levers — a confidence shrink on the rating gap
and an extra draw inflation for cagey group games — and reports log-loss + draw
calibration on each sample, so a change is only adopted if it helps the holdout
out-of-sample, not just the noisy 21-game live slice.

    .venv\\Scripts\\python.exe -m scripts.eval_wc
"""
from __future__ import annotations

import csv
import math
import sys

from backend.config import (ELO_HOME_ADV, HOST_CITY_COUNTRY, WC_CONFIDENCE,
                            WC_GROUP_DRAW_BOOST, WC_HOST_ELO_BONUS, canonical)
from backend.elo import RESULTS_CSV, EloState, ensure_results_csv
from backend.match_model import outcome_probs, params

FRIENDLY_FROM = "2026-03-01"          # immediate pre-WC tune-up window
WC_KICKOFF = "2026-06-11"


def _result(h: int, a: int) -> str:
    return "home" if h > a else "away" if a > h else "draw"


# ---- sample builders --------------------------------------------------------
def historical_samples() -> tuple[list, list]:
    """Replay Elo over all of history; collect (d, result) for every World Cup
    match and for the recent pre-WC friendly window."""
    ensure_results_csv()
    state = EloState()
    wc, fr = [], []
    with RESULTS_CSV.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            hs, as_ = row["home_score"], row["away_score"]
            if hs in ("NA", "") or as_ in ("NA", ""):
                continue
            h, a = canonical(row["home_team"]), canonical(row["away_team"])
            nt = row["neutral"].upper() == "TRUE"
            H, A = int(float(hs)), int(float(as_))
            d = state.rating[h] + (0 if nt else ELO_HOME_ADV) - state.rating[a]
            tour, date = row["tournament"], row["date"]
            if tour == "FIFA World Cup" and date < WC_KICKOFF:
                wc.append({"d": d, "r": _result(H, A), "date": date})
            elif tour == "Friendly" and date >= FRIENDLY_FROM and date < WC_KICKOFF:
                fr.append({"d": d, "r": _result(H, A), "date": date})
            state.apply(h, a, H, A, tour, nt)
    return wc, fr


def live_wc_samples() -> list:
    """WC2026 matches already played, reconstructed from the pre-kickoff strength
    snapshot in the DB (same path tune_urgency uses), plus group urgency."""
    from backend import db as dbm
    from backend.urgency import match_urgency
    try:
        con = dbm.connect()
    except Exception:
        return []
    urg = match_urgency(con)
    out = []
    for m in con.execute(
            "SELECT * FROM matches WHERE home_score IS NOT NULL "
            "AND home_team IS NOT NULL ORDER BY kickoff_utc"):
        pre = con.execute("SELECT id FROM runs WHERE ts < ? ORDER BY ts DESC "
                          "LIMIT 1", (m["kickoff_utc"],)).fetchone()
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
        out.append({"d": d, "r": _result(m["home_score"], m["away_score"]),
                    "stage": m["stage"], "urg": urg.get(m["number"], (0.35, 0.35))})
    con.close()
    return out


# ---- scoring ----------------------------------------------------------------
def score(samples: list, conf: float, draw_boost: float) -> dict:
    """Log-loss / accuracy / mean-vs-actual draw calibration under a candidate
    (confidence shrink, draw boost)."""
    n = len(samples)
    if not n:
        return {"n": 0}
    ll = hits = mpd = adraw = 0.0
    for s in samples:
        fc = outcome_probs(s["d"] * conf, draw_boost=draw_boost)
        p = {"home": fc["home"], "draw": fc["draw"], "away": fc["away"]}
        ll += -math.log(max(1e-9, p[s["r"]]))
        hits += max(p, key=p.get) == s["r"]
        mpd += p["draw"]
        adraw += s["r"] == "draw"
    return {"n": n, "logloss": ll / n, "acc": hits / n,
            "mean_pdraw": mpd / n, "act_draw": adraw / n}


def line(tag: str, r: dict) -> str:
    if not r.get("n"):
        return f"  {tag:11s}  (no samples)"
    return (f"  {tag:11s} n={r['n']:4d}  ll={r['logloss']:.4f}  acc={r['acc']:.3f}"
            f"  P(draw)={r['mean_pdraw']:.3f} vs actual {r['act_draw']:.3f}")


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    wc, fr = historical_samples()
    live = live_wc_samples()
    wc_recent = [s for s in wc if s["date"] >= "2002-01-01"]

    base_db = params().get("draw_boost", 0.0)
    print(f"current params: WC_CONFIDENCE={WC_CONFIDENCE}  base draw_boost="
          f"{base_db}  WC_GROUP_DRAW_BOOST={WC_GROUP_DRAW_BOOST}\n")

    print("=== current production params ===")
    print(line("hist_wc", score(wc, WC_CONFIDENCE, base_db)))
    print(line("hist_wc'02+", score(wc_recent, WC_CONFIDENCE, base_db)))
    print(line("friendly", score(fr, 1.0, base_db)))
    # live: production also applies group draw boost + urgency; approximate with
    # the same confidence + combined draw boost the production path will use.
    print(line("live_wc", score(live, WC_CONFIDENCE, base_db + WC_GROUP_DRAW_BOOST)))

    print("\n=== sweep: confidence x draw_boost on hist_wc'02+ (the calibration "
          "set) ===")
    print(f"  {'conf':>5} {'boost':>6} {'logloss':>8} {'acc':>6} "
          f"{'P(draw)':>8} {'live_ll':>8}")
    best = (None, 9e9)
    for conf in (1.00, 0.92, 0.88, 0.84, 0.80):
        for db in (0.05, 0.10, 0.15, 0.20, 0.25):
            r = score(wc_recent, conf, db)
            lr = score(live, conf, db)
            flag = ""
            if r["logloss"] < best[1]:
                best = ((conf, db), r["logloss"])
                flag = ""
            print(f"  {conf:5.2f} {db:6.2f} {r['logloss']:8.4f} {r['acc']:6.3f} "
                  f"{r['mean_pdraw']:8.3f} {lr['logloss']:8.4f}")
    print(f"\nbest on hist_wc'02+: conf={best[0][0]} draw_boost={best[0][1]} "
          f"(logloss {best[1]:.4f})")
    print("\nNote: adopt only if it also holds on live_wc + friendly and doesn't "
          "regress the general bench (scripts/evaluate.py).")


if __name__ == "__main__":
    main()
