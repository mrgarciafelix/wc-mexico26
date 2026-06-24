"""Systematic match-prediction evaluation harness.

Replays the Elo history ONCE (cached to data/eval_samples.json), then scores any
number of model variants over a held-out window. Adding a variant is one line:
a (name, predict_fn) pair where predict_fn(sample, base) -> (p_home, p_draw, p_away).
This is the bench every match-accuracy idea gets run on before it can ship.

The discipline that keeps us honest: a variant is only "better" if a paired
bootstrap 95% CI on its per-match log-loss improvement **excludes zero**. Point
estimates that move log-loss by 0.01% are noise, not signal — the column to read
is `95% CI`, not `Δ%`.

    .venv\\Scripts\\python.exe -m scripts.evaluate
"""
from __future__ import annotations

import csv
import json
import math
import sys

import numpy as np

from backend.config import DATA, ELO_HOME_ADV, FORM_CAP, canonical
from backend.elo import RESULTS_CSV, EloState, ensure_results_csv
from backend.match_model import outcome_probs

CACHE = DATA / "eval_samples.json"
TEST_FROM, TEST_TO = "2022-01-01", "2026-06-01"
N_BOOT = 2000              # bootstrap resamples for the log-loss CI
RECENT_FROM = "2024-01-01"  # strict-recency sub-window (post param-fit drift)


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
    """Per game in the test window: pre-match Elo diff (incl. home adv), each
    side's form (last-10 Elo delta sum), tournament category, date, actual
    result. Storing per-side form lets a variant apply the *exact* production
    form transform (capped). 'fh' presence signals the current schema; an older
    cache without it is rebuilt automatically."""
    if CACHE.exists():
        try:
            data = json.loads(CACHE.read_text(encoding="utf-8"))
            if data and "fh" in data[0]:
                return data
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
                fh, fa = state.form(h), state.form(a)
                out.append({"d": round(d, 1), "fh": round(fh, 1),
                            "fa": round(fa, 1), "fd": round(fh - fa, 1),
                            "cat": category(row["tournament"]), "date": row["date"],
                            "r": "home" if H > A else "away" if A > H else "draw"})
            state.apply(h, a, H, A, row["tournament"], nt)
    CACHE.write_text(json.dumps(out), encoding="utf-8")
    return out


def score(samples: list[dict], fn, base: dict) -> tuple[np.ndarray, float, float]:
    """Per-sample log-loss array (for bootstrapping) + accuracy + Brier."""
    ll = np.empty(len(samples))
    hits = brier = 0.0
    for i, s in enumerate(samples):
        ph, pd, pa = fn(s, base)
        p = {"home": ph, "draw": pd, "away": pa}
        ll[i] = -math.log(max(1e-9, p[s["r"]]))
        brier += sum((p[k] - (s["r"] == k)) ** 2 for k in p)
        hits += max(p, key=p.get) == s["r"]
    n = len(samples)
    return ll, hits / n, brier / n


# ---- model variants ---------------------------------------------------------
def shrink(probs, base, a):
    return tuple((1 - a) * p + a * base[k] for p, k in zip(probs, ("home", "draw", "away")))


def _form_adj(f: float, w: float) -> float:
    """Production form transform: weight * (last-10 Elo delta sum), capped."""
    return max(-FORM_CAP, min(FORM_CAP, w * f))


def base_pred(s, base, *, conf=1.0, db=None, shrink_a=0.0, friendly_a=0.0,
              form_w=0.0):
    d = s["d"]
    if form_w:
        d += _form_adj(s["fh"], form_w) - _form_adj(s["fa"], form_w)
    fc = outcome_probs(d * conf, draw_boost=db)
    p = (fc["home"], fc["draw"], fc["away"])
    a = shrink_a + (friendly_a if s["cat"] == "friendly" else 0.0)
    if a:
        p = shrink(p, base, a)
    tot = sum(p)
    return tuple(x / tot for x in p)


def _cat_conf(s, b, cat, conf):
    """Apply a confidence shrink only to one tournament category."""
    return base_pred(s, b, conf=conf) if s["cat"] == cat else base_pred(s, b)


VARIANTS = {
    "baseline (current)":     lambda s, b: base_pred(s, b),
    "no draw_boost":          lambda s, b: base_pred(s, b, db=0.0),
    # --- form feature (validates the production FORM_WEIGHT=0.5) -------------
    "form w=0.10":            lambda s, b: base_pred(s, b, form_w=0.10),
    "form w=0.25":            lambda s, b: base_pred(s, b, form_w=0.25),
    "form w=0.50 (prod)":     lambda s, b: base_pred(s, b, form_w=0.50),
    "form w=0.75":            lambda s, b: base_pred(s, b, form_w=0.75),
    "form w=-0.25 (revert)":  lambda s, b: base_pred(s, b, form_w=-0.25),
    # --- global confidence ---------------------------------------------------
    "confidence x0.95":       lambda s, b: base_pred(s, b, conf=0.95),
    "shrink-to-base a=0.03":  lambda s, b: base_pred(s, b, shrink_a=0.03),
    # --- per-category (the weak tournaments from the breakdown) --------------
    "WC conf x0.88":          lambda s, b: _cat_conf(s, b, "world_cup", 0.88),
    "WC shrink a=0.10":       lambda s, b: base_pred(s, b, shrink_a=0.10) if s["cat"] == "world_cup" else base_pred(s, b),
    "continental conf x0.93": lambda s, b: _cat_conf(s, b, "continental", 0.93),
    "friendly shrink a=0.10": lambda s, b: base_pred(s, b, friendly_a=0.10),
}


def main() -> None:
    try:                                  # Windows consoles default to cp1252
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    S = build_samples()
    base = {k: sum(s["r"] == k for s in S) / len(S) for k in ("home", "draw", "away")}
    print(f"n={len(S)}  base rates "
          f"{ {k: round(v, 3) for k, v in base.items()} }\n")

    scored = {name: score(S, fn, base) for name, fn in VARIANTS.items()}
    base_ll = scored["baseline (current)"][0]
    b_mean = base_ll.mean()

    # one shared set of bootstrap indices -> paired, comparable CIs across rows
    idx = np.random.default_rng(0).integers(0, len(S), size=(N_BOOT, len(S)))

    print(f"{'variant':24s} {'logloss':>8} {'Δ%':>6} {'acc':>6} "
          f"{'mean Δll':>9} {'95% CI':>18}")
    for name, (ll, acc, _brier) in scored.items():
        dpct = (b_mean - ll.mean()) / b_mean * 100
        diff = base_ll - ll                       # +ve = variant beats baseline
        boot = diff[idx].mean(axis=1)
        lo, hi = np.percentile(boot, [2.5, 97.5])
        sig = " SIG" if (lo > 0 or hi < 0) else ""
        print(f"{name:24s} {ll.mean():8.4f} {dpct:+6.2f} {acc:6.3f} "
              f"{diff.mean():+9.5f} [{lo:+.5f},{hi:+.5f}]{sig}")

    print("\n* SIG = 95% bootstrap CI on per-match log-loss gain excludes 0 "
          "(real signal, not noise).")

    print("\nbaseline by tournament type:")
    for cat in ("world_cup", "continental", "qualifier", "friendly", "other"):
        sub = [s for s in S if s["cat"] == cat]
        if len(sub) > 40:
            ll, acc, _ = score(sub, VARIANTS["baseline (current)"], base)
            print(f"  {cat:12s} n={len(sub):4d}  logloss={ll.mean():.4f}  acc={acc:.3f}")

    recent = [s for s in S if s["date"] >= RECENT_FROM]
    if len(recent) > 100:
        ll, acc, _ = score(recent, VARIANTS["baseline (current)"], base)
        print(f"\nbaseline on strict-recency window {RECENT_FROM}..{TEST_TO}: "
              f"n={len(recent)}  logloss={ll.mean():.4f}  acc={acc:.3f}")


if __name__ == "__main__":
    main()
