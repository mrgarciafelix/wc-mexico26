"""Systematic match-prediction evaluation harness.

Replays the Elo history ONCE (cached to data/eval_samples.json), then scores any
number of model variants over a held-out window. Adding a variant is one line:
a (name, predict_fn) pair where predict_fn(sample, base) -> (p_home, p_draw, p_away).
This is the bench every match-accuracy idea gets run on before it can ship.

    .venv\\Scripts\\python.exe -m scripts.evaluate
"""
from __future__ import annotations

import csv
import json
import math

from backend.config import DATA, ELO_HOME_ADV, canonical
from backend.elo import RESULTS_CSV, EloState, ensure_results_csv
from backend.match_model import outcome_probs

CACHE = DATA / "eval_samples.json"
TEST_FROM, TEST_TO = "2022-01-01", "2026-06-01"


def category(tournament: str) -> str:
    t = tournament.lower()
    if "world cup" in t and "quali" not in t:
        return "world_cup"
    if "quali" in t:
        return "qualifier"
    if "friendly" in t:
        return "friendly"
    if any(w in t for w in ("euro", "copa", "nations", "cup of nations", "asian", "gold")):
        return "continental"
    return "other"


def build_samples() -> list[dict]:
    """(pre-match Elo diff, form diff, tournament category, actual result) per game."""
    if CACHE.exists():
        try:
            return json.loads(CACHE.read_text(encoding="utf-8"))
        except Exception:
            pass
    ensure_results_csv()
    state = EloState()
    out = []
    with RESULTS_CSV.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            hs, as_ = row["home_score"], row["away_score"]
            if hs in ("NA", "") or as_ in ("NA", ""):
                continue
            h, a = canonical(row["home_team"]), canonical(row["away_team"])
            nt = row["neutral"].upper() == "TRUE"
            H, A = int(float(hs)), int(float(as_))
            if TEST_FROM <= row["date"] < TEST_TO:
                d = state.rating[h] + (0 if nt else ELO_HOME_ADV) - state.rating[a]
                out.append({"d": round(d, 1),
                            "fd": round(state.form(h) - state.form(a), 1),
                            "cat": category(row["tournament"]),
                            "r": "home" if H > A else "away" if A > H else "draw"})
            state.apply(h, a, H, A, row["tournament"], nt)
    CACHE.write_text(json.dumps(out), encoding="utf-8")
    return out


def metrics(samples: list[dict], fn, base: dict) -> dict:
    ll = brier = hits = 0.0
    for s in samples:
        ph, pd, pa = fn(s, base)
        p = {"home": ph, "draw": pd, "away": pa}
        ll += -math.log(max(1e-9, p[s["r"]]))
        brier += sum((p[k] - (s["r"] == k)) ** 2 for k in p)
        if max(p, key=p.get) == s["r"]:
            hits += 1
    n = len(samples)
    return {"logloss": ll / n, "brier": brier / n, "acc": hits / n, "n": n}


# ---- model variants ---------------------------------------------------------
def shrink(probs, base, a):
    return tuple((1 - a) * p + a * base[k] for p, k in zip(probs, ("home", "draw", "away")))


def base_pred(s, base, *, conf=1.0, db=None, shrink_a=0.0, friendly_a=0.0):
    fc = outcome_probs(s["d"] * conf, draw_boost=db)
    p = (fc["home"], fc["draw"], fc["away"])
    a = shrink_a + (friendly_a if s["cat"] == "friendly" else 0.0)
    if a:
        p = shrink(p, base, a)
    tot = sum(p)
    return tuple(x / tot for x in p)


VARIANTS = {
    "baseline (current)":     lambda s, b: base_pred(s, b),
    "no draw_boost":          lambda s, b: base_pred(s, b, db=0.0),
    "confidence x0.90":       lambda s, b: base_pred(s, b, conf=0.90),
    "confidence x0.95":       lambda s, b: base_pred(s, b, conf=0.95),
    "confidence x1.05":       lambda s, b: base_pred(s, b, conf=1.05),
    "shrink-to-base a=0.03":  lambda s, b: base_pred(s, b, shrink_a=0.03),
    "shrink-to-base a=0.06":  lambda s, b: base_pred(s, b, shrink_a=0.06),
    "friendly shrink a=0.10": lambda s, b: base_pred(s, b, friendly_a=0.10),
    "friendly shrink a=0.20": lambda s, b: base_pred(s, b, friendly_a=0.20),
    "friendly conf x0.85":    lambda s, b: base_pred(s, b, conf=0.85) if s["cat"] == "friendly" else base_pred(s, b),
}


def main() -> None:
    S = build_samples()
    base = {k: sum(s["r"] == k for s in S) / len(S) for k in ("home", "draw", "away")}
    print(f"n={len(S)}  base rates {base}\n")
    rows = [(name, metrics(S, fn, base)) for name, fn in VARIANTS.items()]
    b_ll = rows[0][1]["logloss"]
    print(f"{'variant':24s} {'logloss':>8} {'Δ%':>6} {'acc':>6} {'brier':>7}")
    for name, m in rows:
        d = (b_ll - m["logloss"]) / b_ll * 100
        flag = "  <-- better" if m["logloss"] < b_ll - 1e-5 else ""
        print(f"{name:24s} {m['logloss']:8.4f} {d:+6.2f} {m['acc']:6.3f} {m['brier']:7.4f}{flag}")

    print("\nbaseline by tournament type:")
    for cat in ("world_cup", "continental", "qualifier", "friendly", "other"):
        sub = [s for s in S if s["cat"] == cat]
        if len(sub) > 40:
            m = metrics(sub, VARIANTS["baseline (current)"], base)
            print(f"  {cat:12s} n={m['n']:4d}  logloss={m['logloss']:.4f}  acc={m['acc']:.3f}")


if __name__ == "__main__":
    main()
