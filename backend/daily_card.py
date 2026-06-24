"""Zero-input DAILY PARLAY CARD (v2): two cross-game tickets, nothing to decide.

The v1 card mixed per-game single picks, same-game boosters and parlays. Singles
on short favourites return almost nothing, so v2 is **parlays only**:

  • Parlay of the Day — the model's bankers across today's slate plus one embedded
    player-prop kicker (e.g. "England win + France win + Mbappé anytime"). Upside.
  • Safe Parlay — only the strongest favourites, sized so that IF it lands it
    covers the whole day's outlay (the "come clean / net ≥ 0" target — a target,
    NOT a guarantee: the safe leg can still lose).

Player props are folded in as legs, not a separate section. The joint probability
respects correlation: legs in the SAME match are scored off that match's exact
score matrix (a scorer's chance is conditioned on his team's goals via Poisson
thinning, so "team wins" and "team's striker scores" are not double-counted);
legs in DIFFERENT matches multiply as independent. Nested props are de-duplicated
by family so a ticket never stacks "1+ save" with "2+ saves".

Staking is model-chosen and bounded [10, 50] pesos per ticket — no bankroll, no
Kelly, no inputs. Safer ticket → bigger stake. A Monte-Carlo projection of the
day's P/L (expected, prob-green, worst/realistic/best) rides on top.

Honest health warning (kept in the copy): a high hit-rate favourite parlay is
NOT guaranteed profit — parlaying favourites is −EV because the vig compounds.
Only legs flagged `value` (model_p × odds > 1) actually beat a real market line.
"""
from __future__ import annotations

import math

import numpy as np

from . import props as propmod
from .match_model import MAX_GOALS, score_matrix

STAKE_MIN = 10.0          # pesos: floor stake for the riskiest ticket
STAKE_MAX = 50.0          # pesos: ceiling stake for the safest ticket
SAFE_MIN_P = 0.60         # a result leg is "safe" if the model gives it >= 60%
DAY_MAX_LEGS = 5          # cap the day parlay so it can realistically land
SAFE_MAX_LEGS = 4         # cap the safe parlay
SCORER_TOPN = 3           # scoring threats considered per team
MC_SIMS = 20000           # Monte-Carlo paths for the day projection

_G = np.add.outer(np.arange(MAX_GOALS + 1), np.arange(MAX_GOALS + 1))  # i+j grid


# --------------------------------------------------------------- odds lookup --
def _odds_lookup(odds: dict, market: str, selection: str, p: float
                 ) -> tuple[float, bool]:
    """Real consensus odds if we have them, else the model's fair price."""
    o = odds.get(f"{market}|{selection}")
    if o and o > 1.0:
        return float(o), True
    return (round(1.0 / p, 2) if p > 0 else 0.0), False


# ----------------------------------------------------------- leg constructors --
def _result_leg(no, side, home, away, p, o, live) -> dict:
    team = home if side == "home" else away if side == "away" else None
    text = "Draw" if side == "draw" else f"{team} to win"
    return {"key": f"m{no}:res:{side}", "kind": "result", "match_no": no,
            "text": text, "model_p": round(p, 4), "odds": o, "live_odds": live,
            "value": bool(o and p * o > 1.0), "family": f"m{no}:1x2",
            "side": side, "home": home, "away": away, "team": team,
            "verifiable": True}


_TOTAL_LABEL = {"over1.5": ("over", 1.5), "under1.5": ("under", 1.5),
                "over2.5": ("over", 2.5), "under2.5": ("under", 2.5)}


def _total_leg(no, tkey, p) -> dict:
    direction, line = _TOTAL_LABEL[tkey]
    return {"key": f"m{no}:tot:{tkey}", "kind": "total", "match_no": no,
            "text": f"{direction.capitalize()} {line} goals", "model_p": round(p, 4),
            "odds": round(1.0 / p, 2) if p > 0 else 0.0, "live_odds": False,
            "value": False, "family": f"m{no}:totals",
            "total": [direction, line], "verifiable": True}


def _clean_name(name: str) -> str:
    """Drop squad-list annotations like '( captain )' from a player's name."""
    return name.split("(")[0].strip() if name else name


def _scorer_leg(no, pr, side, share, team) -> dict:
    p = pr["props"]["anytime"]
    name = _clean_name(pr["name"])
    return {"key": f"m{no}:scorer:{pr['id']}", "kind": "scorer", "match_no": no,
            "text": f"{name} to score anytime", "model_p": round(p, 4),
            "odds": round(1.0 / p, 2) if p > 0 else 0.0, "live_odds": False,
            "value": False, "family": f"prop:{pr['id']}:goals",
            "share": share, "n_plus": 1, "scorer_side": side,
            "player": name, "team": team, "verifiable": False}


def _gk_leg(no, gk, team) -> dict:
    p = gk["props"]["saves2"]
    name = _clean_name(gk["name"])
    return {"key": f"m{no}:saves:{gk['id']}", "kind": "saves", "match_no": no,
            "text": f"{name} 2+ saves", "model_p": round(p, 4),
            "odds": round(1.0 / p, 2) if p > 0 else 0.0, "live_odds": False,
            "value": False, "family": f"prop:{gk['id']}:saves",
            "player": gk["name"], "team": team, "verifiable": False}


def _totals(M: np.ndarray) -> dict[str, float]:
    return {"over1.5": float(M[_G > 1.5].sum()), "under1.5": float(M[_G < 1.5].sum()),
            "over2.5": float(M[_G > 2.5].sum()), "under2.5": float(M[_G < 2.5].sum())}


def _shot_leg(no, pr, side, team) -> dict:
    """A high-volume SHOTS prop (2+ shots) — priced from real per-90 volume when
    the player is covered, so a streaky high-volume shooter reads correctly."""
    p = pr["props"]["shots2"]
    name = _clean_name(pr["name"])
    return {"key": f"m{no}:shots:{pr['id']}", "kind": "shots", "match_no": no,
            "text": f"{name} 2+ shots", "model_p": round(p, 4),
            "odds": round(1.0 / p, 2) if p > 0 else 0.0, "live_odds": False,
            "value": False, "family": f"prop:{pr['id']}:shots",
            "player": name, "team": team, "verifiable": False,
            "src": pr.get("shots_src", "goals")}


def _candidate_legs(m: dict, odds: dict, squads: dict | None,
                    shots_lookup: dict | None = None):
    """All bettable legs for one fixture, grouped by kind, plus its score matrix."""
    fc = m["forecast"]
    no, home, away = m["number"], m["home_team"], m["away_team"]
    M = score_matrix(fc["exp_goals_home"], fc["exp_goals_away"])
    out = {"result": [], "total": [], "scorer": [], "shot": [], "other": []}
    for side, p in (("home", fc["home"]), ("draw", fc["draw"]), ("away", fc["away"])):
        o, live = _odds_lookup(odds, f"match:{no}", side, p)
        out["result"].append(_result_leg(no, side, home, away, p, o, live))
    tots = _totals(M)
    for tkey, p in tots.items():
        out["total"].append(_total_leg(no, tkey, p))
    if squads:
        lam = {"home": fc["exp_goals_home"], "away": fc["exp_goals_away"]}
        for team, side in ((home, "home"), (away, "away")):
            sq = squads.get(team)
            if not sq:
                continue
            tl = lam[side]
            for pr in propmod.outfield_props(sq, tl, SCORER_TOPN, shots_lookup):
                share = pr["exp_goals"] / tl if tl > 0 else 0.0
                if share > 0:
                    out["scorer"].append(_scorer_leg(no, pr, side, share, team))
                sp = pr["props"].get("shots2", 0.0)
                if 0.45 <= sp <= 0.97:        # useful band: skip near-certain / noise
                    out["shot"].append(_shot_leg(no, pr, side, team))
            gk = propmod.gk_props(sq, lam["away" if side == "home" else "home"])
            if gk:
                out["other"].append(_gk_leg(no, gk, team))
    return out, M


# ----------------------------------------------------- correlation-aware prob --
def _scorer_tail(k: np.ndarray, s: float, n: int) -> np.ndarray:
    """P(player scores >= n | his team scored k goals), via Poisson thinning:
    given k team goals, the player's are Binomial(k, share)."""
    q = 1.0 - s
    if n == 1:
        pk = 1.0 - q ** k
    else:                                   # n == 2
        pk = 1.0 - q ** k - k * s * q ** np.clip(k - 1, 0, None)
        pk = np.where(k >= 2, pk, 0.0)
    return np.clip(pk, 0.0, 1.0)


def _cond_grid(leg: dict, N: int) -> np.ndarray:
    """P(leg | home=i, away=j) as an N×N grid (rows=home goals, cols=away goals)."""
    I = np.arange(N)[:, None]
    J = np.arange(N)[None, :]
    if leg["kind"] == "result":
        g = I > J if leg["side"] == "home" else J > I if leg["side"] == "away" else I == J
        return g.astype(float)
    if leg["kind"] == "total":
        direction, line = leg["total"]
        tot = I + J
        return (tot > line if direction == "over" else tot < line).astype(float)
    # scorer: thin against the relevant side's goals
    k = np.arange(N)
    pk = _scorer_tail(k, leg["share"], leg["n_plus"])
    if leg["scorer_side"] == "home":
        return np.repeat(pk[:, None], N, axis=1)
    return np.repeat(pk[None, :], N, axis=0)


def _match_joint(legs: list[dict], M: np.ndarray) -> float:
    """Joint probability of several legs in the SAME match. Result/total/scorer
    legs are scored off the exact score matrix (correlation honest); shots/saves
    legs are weakly tied to the result given we're in the same game, so they
    multiply in as conditionally independent (a documented approximation)."""
    N = M.shape[0]
    matrix_legs = [l for l in legs if l["kind"] in ("result", "total", "scorer")]
    indep_legs = [l for l in legs if l["kind"] in ("sot", "saves", "shots")]
    if matrix_legs:
        cond = np.ones((N, N))
        for l in matrix_legs:
            cond = cond * _cond_grid(l, N)
        base = float((M * cond).sum())
    else:
        base = 1.0
    for l in indep_legs:
        base *= l["model_p"]
    return base


def _parlay_prob(legs: list[dict], mats: dict[int, np.ndarray]) -> float:
    """Cross-match legs multiply (independent); same-match legs go through the
    correlation-aware joint above."""
    by_match: dict[int, list] = {}
    for l in legs:
        by_match.setdefault(l["match_no"], []).append(l)
    p = 1.0
    for no, ls in by_match.items():
        p *= _match_joint(ls, mats[no])
    return p


# ------------------------------------------------------------------ staking ---
def _stake_for(model_p: float, value_edge: float = 0.0) -> float:
    """Confidence → stake in [10, 50] pesos. Safer ticket (higher model_p) stakes
    bigger; a genuine value edge nudges it up. Kelly-flavoured but capped, no
    bankroll input."""
    lo, hi = 0.05, 0.50
    frac = max(0.0, min(1.0, (model_p - lo) / (hi - lo)))
    s = STAKE_MIN + (STAKE_MAX - STAKE_MIN) * frac
    if value_edge > 0:
        s *= 1.0 + min(value_edge, 0.20)
    return float(round(max(STAKE_MIN, min(STAKE_MAX, s))))


# --------------------------------------------------------------- projection ---
def _project(tickets: list[dict], mats: dict[int, np.ndarray]) -> dict:
    """Monte-Carlo the day's P/L across the staked tickets, correlation-honest:
    one score is drawn per match (shared by every ticket that touches it), then
    each leg is evaluated on that path. Returns expected P/L, prob of finishing
    green, and worst/realistic/best (5th/50th/95th percentile) cases."""
    rng = np.random.default_rng(20260623)
    sims: dict[int, dict] = {}
    for no, M in mats.items():
        flat = (M / M.sum()).ravel()
        N = M.shape[0]
        idx = rng.choice(flat.size, size=MC_SIMS, p=flat)
        sims[no] = {"i": idx // N, "j": idx % N}
    cache: dict[str, np.ndarray] = {}

    def hits(leg: dict) -> np.ndarray:
        if leg["key"] in cache:
            return cache[leg["key"]]
        s = sims[leg["match_no"]]
        i, j = s["i"], s["j"]
        k = leg["kind"]
        if k == "result":
            h = (i > j) if leg["side"] == "home" else \
                (j > i) if leg["side"] == "away" else (i == j)
        elif k == "total":
            direction, line = leg["total"]
            tot = i + j
            h = tot > line if direction == "over" else tot < line
        elif k == "scorer":
            kk = i if leg["scorer_side"] == "home" else j
            h = rng.binomial(kk, leg["share"]) >= leg["n_plus"]
        else:                               # sot / saves: independent Bernoulli
            h = rng.random(MC_SIMS) < leg["model_p"]
        cache[leg["key"]] = h
        return h

    pnl = np.zeros(MC_SIMS)
    for t in tickets:
        land = np.ones(MC_SIMS, dtype=bool)
        for l in t["legs"]:
            land &= hits(l)
        pnl += np.where(land, t["stake"] * (t["combined_odds"] - 1.0), -t["stake"])
    total_stake = float(sum(t["stake"] for t in tickets))
    return {
        "total_stake": round(total_stake, 2),
        "exp_pnl": round(float(pnl.mean()), 2),
        "p_green": round(float((pnl >= 0).mean()), 4),
        "worst": round(float(np.percentile(pnl, 5)), 2),
        "realistic": round(float(np.percentile(pnl, 50)), 2),
        "best": round(float(np.percentile(pnl, 95)), 2),
        "sims": MC_SIMS,
    }


# --------------------------------------------------------- ticket assembly ----
def _ticket(legs: list[dict], label: str, mats: dict, kind: str) -> dict | None:
    legs = _dedup(legs)
    if len(legs) < 2:
        return None
    O = 1.0
    for l in legs:
        O *= l["odds"]
    p = _parlay_prob(legs, mats)
    return {
        "label": label, "kind": kind, "legs": legs, "n_legs": len(legs),
        "combined_odds": round(O, 2), "model_p": round(p, 4),
        "value": bool(p * O > 1.0),
        "live_odds": any(l["live_odds"] for l in legs),
        "has_props": any(not l["verifiable"] for l in legs),
    }


def _dedup(legs: list[dict]) -> list[dict]:
    """Keep one leg per family (never stack two outcomes of the same market, or
    nested props like 1+/2+ saves on the same player)."""
    seen, out = set(), []
    for l in legs:
        if l["family"] in seen:
            continue
        seen.add(l["family"])
        out.append(l)
    return out


def _best_result(game_legs: dict) -> dict:
    return max(game_legs["result"], key=lambda l: l["model_p"])


def _stake_ticket(t: dict | None) -> None:
    if not t:
        return
    edge = max(0.0, t["model_p"] * t["combined_odds"] - 1.0)
    t["stake"] = _stake_for(t["model_p"], edge)
    t["to_return"] = round(t["stake"] * t["combined_odds"], 2)
    t["exp_profit"] = round(t["stake"] * (t["model_p"] * t["combined_odds"] - 1.0), 2)


def _choose_safe(cands: list[dict], day_stake: float) -> dict | None:
    """The Safe Parlay's whole point is to LAND, so take the most-likely build
    (fewest legs / strongest favourites). Coverage of the day's outlay is then a
    best-effort target sized on top (see _size_safe_to_cover) — never a reason to
    lengthen the ticket and sink its landing probability."""
    if not cands:
        return None
    return max(cands, key=lambda t: t["model_p"])


def _size_safe_to_cover(safe: dict, day_stake: float) -> None:
    """Raise the safe stake (within the cap) until its win covers the whole day:
    safe·(odds−1) ≥ safe + day  ⇔  safe ≥ day / (odds−2), for odds > 2."""
    o = safe["combined_odds"]
    if o > 2.0:
        needed = day_stake / (o - 2.0)
        safe["stake"] = float(min(STAKE_MAX, max(safe["stake"], math.ceil(needed))))
    safe["to_return"] = round(safe["stake"] * o, 2)
    safe["exp_profit"] = round(safe["stake"] * (safe["model_p"] * o - 1.0), 2)


def daily_card(matches: list[dict], odds: dict, squads: dict | None = None,
               shots_lookup: dict | None = None) -> dict:
    """matches: the /api/matches list (with `forecast`). odds: {market|sel:
    decimal_odds} of any live consensus odds. squads: {team: [player rows]} for
    embedded props (optional). shots_lookup: {norm name: shots/90} so shot props
    are priced from real volume. Returns the parlay-only daily card."""
    upcoming = [m for m in matches
                if m.get("forecast") and m.get("home_team") and m.get("away_team")
                and m.get("home_score") is None and m.get("kickoff_utc")]
    upcoming.sort(key=lambda m: m["kickoff_utc"])
    if not upcoming:
        return {"slate": None, "tickets": [], "day_parlay": None,
                "safe_parlay": None, "projection": None, "come_clean": None,
                "summary": {"n_games": 0, "note": "No upcoming matches — check "
                            "back before the next match day."}}

    slate_date = upcoming[0]["kickoff_utc"][:10]
    slate = [m for m in upcoming if m["kickoff_utc"][:10] == slate_date]

    mats: dict[int, np.ndarray] = {}
    per_game: dict[int, dict] = {}
    bankers: list[dict] = []
    scorers: list[dict] = []
    shot_legs: list[dict] = []
    for m in slate:
        legs, M = _candidate_legs(m, odds, squads, shots_lookup)
        mats[m["number"]] = M
        per_game[m["number"]] = legs
        bankers.append(_best_result(legs))
        scorers.extend(legs["scorer"])
        shot_legs.extend(legs["shot"])

    bankers.sort(key=lambda l: -l["model_p"])
    scorers.sort(key=lambda l: -l["model_p"])
    shot_legs.sort(key=lambda l: -l["model_p"])

    def _pid(leg: dict) -> str:
        return leg["key"].rsplit(":", 1)[-1]

    # --- Parlay of the Day: bankers + a scorer (upside) + a high-volume shots
    #     prop (the anchor) — different players, priced from real shot volume -----
    if len(slate) >= 2:
        kicker, used = [], set()
        if scorers:
            kicker.append(scorers[0]); used.add(_pid(scorers[0]))
        sh = next((l for l in shot_legs if _pid(l) not in used), None)
        if sh:
            kicker.append(sh)
        day_legs = bankers[:DAY_MAX_LEGS - len(kicker)] + kicker
        day_parlay = _ticket(day_legs, "Parlay of the Day", mats, "cross")
    else:                                   # one-game slate → same-game parlay
        g = per_game[slate[0]["number"]]
        sg = [_best_result(g)]
        if g["total"]:
            sg.append(max(g["total"], key=lambda l: l["model_p"]))
        if g["scorer"]:
            sg.append(g["scorer"][0])
        if g["shot"]:
            sg.append(max(g["shot"], key=lambda l: l["model_p"]))
        day_parlay = _ticket(sg, "Parlay of the Day", mats, "same-game")

    _stake_ticket(day_parlay)
    day_stake = day_parlay["stake"] if day_parlay else 0.0

    # --- Safe Parlay: strongest favourites, leg-count chosen so it can cover ---
    # Favourites (>=SAFE_MIN_P) preferred; fall back to the day's two strongest.
    # A safe parlay only "covers" the day if its odds clear evens AND the stake
    # needed stays within the cap. Try 2- and 3-leg builds and pick the one that
    # covers with the highest landing probability; else the most likely 2-leg.
    pool = [l for l in bankers if l["model_p"] >= SAFE_MIN_P]
    if len(pool) < 2:
        pool = bankers
    cands = []
    for k in (2, 3):
        if len(pool) >= k:
            t = _ticket(pool[:k], "Safe Parlay", mats, "cross")
            if t:
                cands.append(t)
    safe_parlay = _choose_safe(cands, day_stake)
    _stake_ticket(safe_parlay)

    # --- "come clean": size the safe ticket so its win covers the day's outlay -
    come_clean = None
    if day_parlay and safe_parlay:
        _size_safe_to_cover(safe_parlay, day_stake)
        odds_s = safe_parlay["combined_odds"]
        total_outlay = round(day_stake + safe_parlay["stake"], 2)
        safe_profit = round(safe_parlay["stake"] * (odds_s - 1.0), 2)
        come_clean = {
            "covered": bool(safe_profit >= total_outlay),
            "total_outlay": total_outlay,
            "safe_profit": safe_profit,
            "safe_p": safe_parlay["model_p"],
            "note": ("If the Safe Parlay lands ({:.0%} model), its ${:.0f} profit "
                     "covers the day's ${:.0f} outlay — a target, not a guarantee."
                     .format(safe_parlay["model_p"], safe_profit, total_outlay)),
        }

    tickets = [t for t in (day_parlay, safe_parlay) if t]
    projection = _project(tickets, mats) if tickets else None
    n_value = sum(1 for t in tickets if t["value"])

    return {
        "slate": slate_date,
        "tickets": tickets, "day_parlay": day_parlay, "safe_parlay": safe_parlay,
        "projection": projection, "come_clean": come_clean,
        "summary": {
            "n_games": len(slate),
            "live_odds": any(t["live_odds"] for t in tickets),
            "n_value": n_value,
            "note": ("Two parlays, model-staked {:.0f}–{:.0f} pesos — no inputs. "
                     "Props are model-priced guides (place them at your book). "
                     "Only legs marked VALUE beat a real line; favourite parlays "
                     "are −EV — bet to enjoy, not to chase.").format(
                         STAKE_MIN, STAKE_MAX),
        },
    }
