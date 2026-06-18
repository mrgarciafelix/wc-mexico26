"""Forward prediction log: record the model's *pre-match* 1X2 forecast for every
fixture, then settle it against the real result — building an honest, WC-specific
track record (model precision over time, and exactly what it got wrong).

Played matches are backfilled from the strengths snapshot of the last run before
kickoff, so the recorded prediction is the genuine pre-match one (never
contaminated by the result). Upcoming matches keep their forecast refreshed.
"""
from __future__ import annotations

import json

from . import db as dbm
from .config import DATA, HOST_CITY_COUNTRY, WC_HOST_ELO_BONUS
from .match_model import outcome_probs

PRED_FILE = DATA / "predictions.json"     # committed, so stateless CI persists it
COLS = ("match_number", "ts", "p_home", "p_draw", "p_away", "exp_home",
        "exp_away", "result", "home_score", "away_score", "settled_ts")


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


def _forecast(sh: float, sa: float, city: str, home: str, away: str) -> dict:
    host = HOST_CITY_COUNTRY.get(city)
    d = (sh - sa + (WC_HOST_ELO_BONUS if host == home else 0)
         - (WC_HOST_ELO_BONUS if host == away else 0))
    return outcome_probs(d)


def _result(h: int, a: int) -> str:
    return "home" if h > a else "away" if a > h else "draw"


def ensure_predictions(con) -> None:
    _load_file(con)                       # seed from the committed track record
    runs = con.execute("SELECT id, ts FROM runs ORDER BY ts").fetchall()
    if not runs:
        return
    latest = runs[-1]["id"]
    cache: dict[int, dict] = {}

    def strengths(run_id: int) -> dict:
        if run_id not in cache:
            cache[run_id] = {r["team"]: r["strength"] for r in con.execute(
                "SELECT team, strength FROM strengths WHERE run_id=?", (run_id,))}
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
                fc = _forecast(st[home], st[away], m["city"], home, away)
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
