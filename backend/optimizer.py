"""Bet-combination optimizer: turn model probabilities + bookmaker odds into a
concrete staking plan (value singles + the growth-optimal parlay).

The math (kept identical to frontend/app.js `optimize()` so the published
client-side calculator and the live API agree):

  edge   e = p*o - 1                      expected profit per $1 staked
  Kelly  f = e / (o - 1)                  full-Kelly fraction of bankroll
  growth ~ e^2 / (o - 1)                  Kelly log-growth rate (small-edge)

Parlay of independent legs S:
  combined odds O = prod(o_i)
  combined prob P = prod(p_i)             (independence — enforced via team keys)
  combined edge E = P*O - 1 = prod(1+e_i) - 1
We pick the parlay maximising growth (E^2/(O-1)), i.e. the combination that
compounds the bankroll fastest, not merely the biggest nominal edge.

Correlation guard: two selections may share a parlay only if their underlying
team sets are disjoint (champion:Spain and match Spain-X both touch {Spain} and
therefore conflict). This keeps the independence assumption honest.
"""
from __future__ import annotations

from itertools import combinations


def implied_prob(o: float) -> float:
    return 1.0 / o if o > 1 else 1.0


def edge(p: float, o: float) -> float:
    return p * o - 1.0


def kelly_fraction(p: float, o: float) -> float:
    b = o - 1.0
    if b <= 0:
        return 0.0
    return max(0.0, (p * b - (1.0 - p)) / b)


def growth_rate(p: float, o: float, f: float) -> float:
    """Exact expected log-growth of staking fraction f on (p, o)."""
    import math
    if f <= 0:
        return 0.0
    win = 1.0 + f * (o - 1.0)
    lose = 1.0 - f
    if win <= 0 or lose <= 0:
        return -math.inf
    return p * math.log(win) + (1.0 - p) * math.log(lose)


def optimize(candidates: list[dict], bankroll: float, kelly_mult: float = 0.25,
             *, max_legs: int = 4, min_parlay_p: float = 0.04,
             parlay_pool: int = 12, exposure_cap: float = 0.5,
             max_parlays: int = 3, max_edge: float = 0.20) -> dict:
    """Build a staking plan.

    Each candidate: {market, selection, model_p, decimal_odds, teams(set|list),
                     label?, match_no?}. `teams` is the set of underlying teams a
    selection depends on (used as the correlation key).

    Returns {singles, parlays, summary}. Singles are the Kelly-optimal core
    (every +EV bet, fractional-Kelly sized, total exposure capped). Parlays are
    higher-variance optional tickets ranked by compounding growth.
    """
    pool = []
    for c in candidates:
        p = c.get("model_p")
        o = c.get("decimal_odds")
        if p is None or o is None or o <= 1.0:
            continue
        e = edge(p, o)
        f = kelly_fraction(p, o)
        pool.append({
            **c,
            "teams": set(c.get("teams") or ([c["selection"]])),
            "edge": e,
            "kelly_full": f,
            "implied_p": implied_prob(o),
            "fair_odds": (1.0 / p) if p > 0 else None,
        })

    # Cap the edge: a model "edge" far above the market is almost always model
    # error, not value — so we don't recommend implausible spots.
    value = sorted([c for c in pool if 1e-9 < c["edge"] <= max_edge],
                   key=lambda c: -c["edge"])

    # --- singles: one bet per mutually-exclusive group, Kelly-sized ----------
    # You can only back one outcome of a match, and only one team wins each
    # outright — so keep the best-edge selection per group (no stacking
    # mutually-exclusive bets, which would otherwise be over-staked).
    def mutex_group(c: dict) -> str:
        if c.get("mutex"):
            return c["mutex"]
        m = c["market"]
        return m if m.startswith("match:") else f"outright:{m}"

    seen, dedup = set(), []
    for c in value:
        g = mutex_group(c)
        if g in seen:
            continue
        seen.add(g)
        dedup.append(c)
    singles = [{**c, "stake": bankroll * kelly_mult * c["kelly_full"]} for c in dedup]
    total = sum(s["stake"] for s in singles)
    cap = bankroll * exposure_cap
    scale = cap / total if total > cap and total > 0 else 1.0
    for s in singles:
        s["stake"] = round(s["stake"] * scale, 2)
        s["exp_profit"] = round(s["stake"] * s["edge"], 2)

    # --- parlays: from the same one-per-market value picks as the singles -----
    legs = dedup[:parlay_pool]
    scored = []
    for r in range(2, max_legs + 1):
        for combo in combinations(legs, r):
            teams = set()
            ok = True
            for leg in combo:
                if teams & leg["teams"]:
                    ok = False
                    break
                teams |= leg["teams"]
            if not ok:
                continue
            O = 1.0
            P = 1.0
            for leg in combo:
                O *= leg["decimal_odds"]
                P *= leg["model_p"]
            if P < min_parlay_p:
                continue
            E = P * O - 1.0
            if E <= 0:
                continue
            growth = E * E / (O - 1.0)        # ~ Kelly growth, ranking key
            scored.append((growth, combo, O, P, E))
    scored.sort(key=lambda x: -x[0])

    parlays = []
    seen = set()
    for growth, combo, O, P, E in scored:
        sig = frozenset((l["market"], l["selection"]) for l in combo)
        if sig in seen:
            continue
        seen.add(sig)
        f = kelly_fraction(P, O)
        stake = round(min(bankroll * kelly_mult * f, bankroll * 0.05), 2)
        parlays.append({
            "legs": [{"market": l["market"], "selection": l["selection"],
                      "label": l.get("label", l["selection"]),
                      "decimal_odds": l["decimal_odds"],
                      "model_p": round(l["model_p"], 4),
                      "edge": round(l["edge"], 4),
                      "team": l.get("team"), "kind": l.get("kind")} for l in combo],
            "combined_odds": round(O, 2),
            "model_p": round(P, 4),
            "implied_p": round(implied_prob(O), 4),
            "edge": round(E, 4),
            "kelly_full": round(f, 4),
            "stake": stake,
            "potential_return": round(stake * O, 2),
            "exp_profit": round(stake * E, 2),
        })
        if len(parlays) >= max_parlays:
            break

    staked = round(sum(s["stake"] for s in singles), 2)
    exp_profit = round(sum(s["exp_profit"] for s in singles), 2)
    summary = {
        "bankroll": bankroll,
        "kelly_mult": kelly_mult,
        "n_value": len(value),
        "singles_stake": staked,
        "singles_exp_profit": exp_profit,
        "singles_roi": round(exp_profit / staked, 4) if staked else 0.0,
        "best_parlay_edge": parlays[0]["edge"] if parlays else None,
    }
    for s in singles:
        s.pop("teams", None)
        s["model_p"] = round(s["model_p"], 4)
        s["edge"] = round(s["edge"], 4)
        s["kelly_full"] = round(s["kelly_full"], 4)
        s["implied_p"] = round(s["implied_p"], 4)
        s["fair_odds"] = round(s["fair_odds"], 2) if s["fair_odds"] else None
    return {"singles": singles, "parlays": parlays, "summary": summary}
