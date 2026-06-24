"""Forward prediction log: record the model's *pre-match* 1X2 forecast for every
fixture, then settle it against the real result — building an honest, WC-specific
track record (model precision over time, and exactly what it got wrong).

Played matches are backfilled from the strengths snapshot of the last run before
kickoff, so the recorded prediction is the genuine pre-match one (never
contaminated by the result). Upcoming matches keep their forecast refreshed.
"""
from __future__ import annotations

import json
import sqlite3

from . import db as dbm
from .config import DATA, HOST_CITY_COUNTRY, WC_CONFIDENCE, WC_HOST_ELO_BONUS
from .match_model import outcome_probs, params, style_multipliers

PRED_FILE = DATA / "predictions.json"     # committed, so stateless CI persists it
STAKE_FILE = DATA / "staking_plan.json"   # committed daily-betting track record
COLS = ("match_number", "ts", "p_home", "p_draw", "p_away", "exp_home",
        "exp_away", "result", "home_score", "away_score", "settled_ts")
PLAN_COLS = ("slate_date", "label", "ts", "stake", "combined_odds", "model_p",
             "n_matches", "legs", "matches", "status", "pnl", "settled_ts")
DAY_COLS = ("slate_date", "ts", "total_stake", "projection", "actual_pnl",
            "settled_ts")


def _load_file(con) -> None:
    if not PRED_FILE.exists():
        return
    try:
        rows = json.loads(PRED_FILE.read_text(encoding="utf-8"))
    except Exception:
        return
    for r in rows:                        # don't clobber a richer local DB row
        con.execute(
            f"INSERT OR IGNORE INTO predictions ({','.join(COLS)}) "
            f"VALUES ({','.join('?'*len(COLS))})", tuple(r.get(c) for c in COLS))
    con.commit()


def _dump_file(con) -> None:
    rows = [dict(r) for r in con.execute(
        "SELECT * FROM predictions ORDER BY match_number")]
    try:
        PRED_FILE.write_text(json.dumps(rows), encoding="utf-8")
    except Exception:
        pass


def _forecast(sh, sa, city: str, home: str, away: str,
              urg=None) -> dict:
    """sh, sa are strengths rows (mapping with 'strength' + style fields) or, for
    backwards compatibility, plain strength floats."""
    from . import urgency
    str_h = sh["strength"] if isinstance(sh, (dict, sqlite3.Row)) else sh
    str_a = sa["strength"] if isinstance(sa, (dict, sqlite3.Row)) else sa
    host = HOST_CITY_COUNTRY.get(city)
    d = (str_h - str_a + (WC_HOST_ELO_BONUS if host == home else 0)
         - (WC_HOST_ELO_BONUS if host == away else 0)) * WC_CONFIDENCE
    gm, lean = 1.0, 0.0
    if urg:
        d, gm, lean = urgency.apply(d, urg["sig_home"], urg["sig_away"],
                                    urg.get("gp", 1))
    style_mult = style_multipliers(_style(sh), _style(sa))
    base_db = params().get("draw_boost", 0.0)
    return outcome_probs(d, draw_boost=base_db + lean, goals_mult=gm,
                         style_mult=style_mult)


def _style(s):
    """(attack, defense) from a strengths row, or neutral for a bare float."""
    if isinstance(s, (dict, sqlite3.Row)):
        return s["style_attack"] or 0.0, s["style_defense"] or 0.0
    return 0.0, 0.0


def _result(h: int, a: int) -> str:
    return "home" if h > a else "away" if a > h else "draw"


def ensure_predictions(con) -> None:
    from . import urgency
    _load_file(con)                       # seed from the committed track record
    runs = con.execute("SELECT id, ts FROM runs ORDER BY ts").fetchall()
    if not runs:
        return
    latest = runs[-1]["id"]
    urg_all = urgency.match_urgency(con)
    cache: dict[int, dict] = {}

    def strengths(run_id: int) -> dict:
        if run_id not in cache:
            cache[run_id] = {r["team"]: r for r in con.execute(
                "SELECT team, strength, style_attack, style_defense "
                "FROM strengths WHERE run_id=?", (run_id,))}
        return cache[run_id]

    for m in con.execute("SELECT * FROM matches WHERE home_team IS NOT NULL "
                         "AND away_team IS NOT NULL"):
        n, home, away = m["number"], m["home_team"], m["away_team"]
        ex = con.execute("SELECT * FROM predictions WHERE match_number=?",
                         (n,)).fetchone()
        played = m["home_score"] is not None
        if played:
            if ex and ex["result"] is None:                 # settle our prediction
                con.execute(
                    "UPDATE predictions SET result=?, home_score=?, away_score=?, "
                    "settled_ts=? WHERE match_number=?",
                    (_result(m["home_score"], m["away_score"]), m["home_score"],
                     m["away_score"], dbm.now(), n))
            elif not ex and m["kickoff_utc"]:               # backfill pre-kickoff
                pre = con.execute("SELECT id, ts FROM runs WHERE ts < ? "
                                  "ORDER BY ts DESC LIMIT 1",
                                  (m["kickoff_utc"],)).fetchone()
                if pre:
                    st = strengths(pre["id"])
                    if home in st and away in st:
                        fc = _forecast(st[home], st[away], m["city"], home, away)
                        con.execute(
                            "INSERT INTO predictions (match_number, ts, p_home, "
                            "p_draw, p_away, exp_home, exp_away, result, home_score, "
                            "away_score, settled_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (n, pre["ts"], fc["home"], fc["draw"], fc["away"],
                             fc["exp_goals_home"], fc["exp_goals_away"],
                             _result(m["home_score"], m["away_score"]),
                             m["home_score"], m["away_score"], dbm.now()))
        else:                                               # upcoming: refresh forecast
            st = strengths(latest)
            if home in st and away in st:
                fc = _forecast(st[home], st[away], m["city"], home, away,
                               urg_all.get(n))
                con.execute(
                    "INSERT INTO predictions (match_number, ts, p_home, p_draw, "
                    "p_away, exp_home, exp_away) VALUES (?,?,?,?,?,?,?) "
                    "ON CONFLICT(match_number) DO UPDATE SET ts=excluded.ts, "
                    "p_home=excluded.p_home, p_draw=excluded.p_draw, "
                    "p_away=excluded.p_away, exp_home=excluded.exp_home, "
                    "exp_away=excluded.exp_away WHERE predictions.result IS NULL",
                    (n, dbm.now(), fc["home"], fc["draw"], fc["away"],
                     fc["exp_goals_home"], fc["exp_goals_away"]))
    con.commit()
    _dump_file(con)                       # persist for stateless CI runs
    try:
        settle_staking_plans(con)         # grade any daily parlays now decided
    except Exception:
        pass


def settled(con) -> dict:
    """Track-record summary + per-match predicted-vs-actual, most recent first."""
    import math
    rows = [dict(r) for r in con.execute(
        "SELECT p.*, m.home_team, m.away_team FROM predictions p "
        "JOIN matches m ON m.number=p.match_number WHERE p.result IS NOT NULL "
        "ORDER BY p.match_number DESC")]
    items, hits, ll, brier = [], 0, 0.0, 0.0
    for r in rows:
        probs = {"home": r["p_home"], "draw": r["p_draw"], "away": r["p_away"]}
        pick = max(probs, key=probs.get)
        hit = pick == r["result"]
        hits += hit
        ll += -math.log(max(1e-9, probs[r["result"]]))
        brier += sum((probs[k] - (r["result"] == k)) ** 2 for k in probs)
        items.append({
            "match": r["match_number"], "home": r["home_team"], "away": r["away_team"],
            "p_home": round(r["p_home"], 3), "p_draw": round(r["p_draw"], 3),
            "p_away": round(r["p_away"], 3), "pick": pick, "result": r["result"],
            "hit": hit, "home_score": r["home_score"], "away_score": r["away_score"]})
    n = len(items)
    return {
        "n": n, "correct": hits,
        "accuracy": round(hits / n, 4) if n else None,
        "logloss": round(ll / n, 4) if n else None,
        "brier": round(brier / n, 4) if n else None,
        "items": items,
    }


# ============================================================================
# Daily staking plan: log the parlay tickets the card recommends, settle them
# against real scores, and keep an honest betting P/L track record (separate
# from the 1X2 model accuracy above).
#
# A ticket is "locked" the moment any of its matches kicks off (until then the
# open plan keeps refreshing with the latest card, mirroring the forecast log).
# Result/total legs settle exactly from the final score; player-prop legs have
# no free results feed, so a ticket containing them settles as 'partial' (its
# verifiable legs are graded; full ticket P/L is left unconfirmed — honest).
# ============================================================================

def _load_staking(con) -> None:
    if not STAKE_FILE.exists():
        return
    try:
        blob = json.loads(STAKE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return
    for p in blob.get("plans", []):
        con.execute(
            f"INSERT OR IGNORE INTO staking_plans ({','.join(PLAN_COLS)}) "
            f"VALUES ({','.join('?'*len(PLAN_COLS))})",
            tuple(p.get(c) for c in PLAN_COLS))
    for d in blob.get("days", []):
        con.execute(
            f"INSERT OR IGNORE INTO staking_days ({','.join(DAY_COLS)}) "
            f"VALUES ({','.join('?'*len(DAY_COLS))})",
            tuple(d.get(c) for c in DAY_COLS))
    con.commit()


def _dump_staking(con) -> None:
    try:
        plans = [dict(r) for r in con.execute(
            "SELECT * FROM staking_plans ORDER BY slate_date, label")]
        days = [dict(r) for r in con.execute(
            "SELECT * FROM staking_days ORDER BY slate_date")]
        STAKE_FILE.write_text(json.dumps({"plans": plans, "days": days}),
                              encoding="utf-8")
    except Exception:
        pass


def _any_played(con, match_nos: list[int]) -> bool:
    if not match_nos:
        return False
    q = ",".join("?" * len(match_nos))
    return bool(con.execute(
        f"SELECT COUNT(*) c FROM matches WHERE number IN ({q}) "
        f"AND home_score IS NOT NULL", match_nos).fetchone()["c"])


def log_staking_plan(con, card: dict) -> None:
    """Record/refresh today's parlay tickets + day projection while still
    pre-kickoff. Called when the snapshot (and thus the card) is built."""
    _load_staking(con)
    slate = card.get("slate")
    if not slate:
        return
    for t in (card.get("day_parlay"), card.get("safe_parlay")):
        if not t:
            continue
        nos = sorted({l["match_no"] for l in t["legs"]})
        row = con.execute("SELECT status FROM staking_plans WHERE slate_date=? "
                          "AND label=?", (slate, t["label"])).fetchone()
        if (row and row["status"] != "open") or _any_played(con, nos):
            continue                       # frozen at kickoff / already settled
        con.execute(
            f"INSERT INTO staking_plans ({','.join(PLAN_COLS)}) "
            f"VALUES ({','.join('?'*len(PLAN_COLS))}) "
            "ON CONFLICT(slate_date, label) DO UPDATE SET "
            "ts=excluded.ts, stake=excluded.stake, "
            "combined_odds=excluded.combined_odds, model_p=excluded.model_p, "
            "n_matches=excluded.n_matches, legs=excluded.legs, "
            "matches=excluded.matches WHERE staking_plans.status='open'",
            (slate, t["label"], dbm.now(), t["stake"], t["combined_odds"],
             t["model_p"], len(nos), json.dumps(t["legs"]), json.dumps(nos),
             "open", None, None))
    proj = card.get("projection")
    if proj:
        con.execute(
            "INSERT INTO staking_days (slate_date, ts, total_stake, projection) "
            "VALUES (?,?,?,?) ON CONFLICT(slate_date) DO UPDATE SET "
            "ts=excluded.ts, total_stake=excluded.total_stake, "
            "projection=excluded.projection WHERE staking_days.settled_ts IS NULL",
            (slate, dbm.now(), proj.get("total_stake"), json.dumps(proj)))
    con.commit()
    _dump_staking(con)


def _leg_hit(leg: dict, hs: int, as_: int) -> bool | None:
    """Did a verifiable (result/total) leg land? None if not verifiable."""
    if leg["kind"] == "result":
        r = "home" if hs > as_ else "away" if as_ > hs else "draw"
        return leg["side"] == r
    if leg["kind"] == "total":
        direction, line = leg["total"]
        tot = hs + as_
        return tot > line if direction == "over" else tot < line
    return None


def settle_staking_plans(con) -> None:
    """Grade every open plan whose matches have all finished, then close out the
    day's P/L once its tickets are decided."""
    changed = False
    for row in con.execute("SELECT * FROM staking_plans WHERE status='open'"):
        nos = json.loads(row["matches"])
        scores = {}
        ready = True
        for n in nos:
            m = con.execute("SELECT home_score, away_score FROM matches "
                            "WHERE number=?", (n,)).fetchone()
            if not m or m["home_score"] is None:
                ready = False
                break
            scores[n] = (m["home_score"], m["away_score"])
        if not ready:
            continue
        legs = json.loads(row["legs"])
        all_verifiable_hit, has_prop = True, False
        for l in legs:
            if l.get("verifiable"):
                hs, as_ = scores[l["match_no"]]
                h = _leg_hit(l, hs, as_)
                l["hit"] = bool(h)
                all_verifiable_hit = all_verifiable_hit and bool(h)
            else:
                has_prop = True
                l["hit"] = None
        if has_prop:
            status, pnl = "partial", None
        elif all_verifiable_hit:
            status = "won"
            pnl = round(row["stake"] * (row["combined_odds"] - 1.0), 2)
        else:
            status, pnl = "lost", round(-row["stake"], 2)
        con.execute("UPDATE staking_plans SET status=?, pnl=?, legs=?, "
                    "settled_ts=? WHERE slate_date=? AND label=?",
                    (status, pnl, json.dumps(legs), dbm.now(),
                     row["slate_date"], row["label"]))
        changed = True

    # close each day once none of its plans are still open
    for d in con.execute("SELECT * FROM staking_days WHERE settled_ts IS NULL"):
        plans = con.execute("SELECT status, pnl FROM staking_plans "
                            "WHERE slate_date=?", (d["slate_date"],)).fetchall()
        if not plans or any(p["status"] == "open" for p in plans):
            continue
        actual = round(sum(p["pnl"] for p in plans if p["pnl"] is not None), 2)
        con.execute("UPDATE staking_days SET actual_pnl=?, settled_ts=? "
                    "WHERE slate_date=?", (actual, dbm.now(), d["slate_date"]))
        changed = True

    if changed:
        con.commit()
        _dump_staking(con)


def staking_record(con) -> dict:
    """Daily-betting summary + per-day tickets for the track-record UI."""
    _load_staking(con)
    plans = [dict(r) for r in con.execute(
        "SELECT * FROM staking_plans ORDER BY slate_date DESC, label")]
    by_day: dict[str, list] = {}
    for p in plans:
        p["legs"] = json.loads(p["legs"])
        p["matches"] = json.loads(p["matches"])
        by_day.setdefault(p["slate_date"], []).append(p)
    settled = [p for p in plans if p["status"] in ("won", "lost")]
    staked = round(sum(p["stake"] for p in settled), 2)
    pnl = round(sum(p["pnl"] for p in settled if p["pnl"] is not None), 2)
    days = []
    for d in con.execute("SELECT * FROM staking_days ORDER BY slate_date DESC"):
        days.append({"slate_date": d["slate_date"],
                     "total_stake": d["total_stake"],
                     "projection": json.loads(d["projection"]) if d["projection"]
                     else None,
                     "actual_pnl": d["actual_pnl"],
                     "settled": d["settled_ts"] is not None,
                     "tickets": by_day.get(d["slate_date"], [])})
    return {
        "n_settled": len(settled),
        "wins": sum(1 for p in settled if p["status"] == "won"),
        "staked": staked, "pnl": pnl,
        "roi": round(pnl / staked, 4) if staked else None,
        "open": sum(1 for p in plans if p["status"] == "open"),
        "days": days,
    }
