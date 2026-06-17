"""Live bookmaker odds from The Odds API (the-odds-api.com).

Free-tier friendly: fetches the World Cup h2h (match 1X2) and outright-winner
markets, keeps the BEST decimal price per selection across covered books, and
caches them to data/live_odds.json (committed, so CI reuses it and only re-hits
the API every few hours). `sync()` loads the cache into the `odds` table, where
build_snapshot's existing "real odds override the sample" logic applies them.

The Odds API covers US/UK/EU/AU books (Pinnacle, Bet365, DraftKings, ...), NOT
Draftea (MX). We surface the best major-book price as a reference — confirm the
actual price on your app before betting.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import httpx

from .config import DATA, USER_AGENT, canonical

BASE = "https://api.the-odds-api.com/v4"
LIVE_PATH = DATA / "live_odds.json"
DEFAULT_REGIONS = "uk"                       # ~17 books; 1 credit per market
REFRESH_HOURS = 6.0                          # don't re-hit the API more often
API_ALIASES = {"USA": "United States",
               "Bosnia & Herzegovina": "Bosnia and Herzegovina"}


def api_key() -> str | None:
    return os.environ.get("WC_ODDS_API_KEY") or os.environ.get("ODDS_API_KEY")


def _canon(name: str) -> str:
    return canonical(API_ALIASES.get(name, name))


def _age_hours(iso: str | None) -> float:
    if not iso:
        return 1e9
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - t).total_seconds() / 3600
    except Exception:
        return 1e9


def _median(xs: list[float]) -> float:
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def _consensus(prices: list[float]) -> list:
    """[median odds, best odds, n] — median is the fair-market reference."""
    return [round(_median(prices), 2), round(max(prices), 2), len(prices)]


def fetch(key: str, regions: str = DEFAULT_REGIONS) -> dict:
    """Best price per selection for upcoming matches (h2h) + the champion market."""
    now = datetime.now(timezone.utc)
    books: set[str] = set()
    remaining = None
    with httpx.Client(timeout=40, headers={"User-Agent": USER_AGENT}) as c:
        ev = c.get(f"{BASE}/sports/soccer_fifa_world_cup/odds/",
                   params={"apiKey": key, "regions": regions, "markets": "h2h",
                           "oddsFormat": "decimal"})
        ev.raise_for_status()
        remaining = ev.headers.get("x-requests-remaining")
        matches = []
        for e in ev.json():
            if datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00")) <= now:
                continue                     # started — odds suspended/stale
            home, away = _canon(e["home_team"]), _canon(e["away_team"])
            prices: dict[str, list] = {"home": [], "draw": [], "away": []}
            for bk in e.get("bookmakers", []):
                books.add(bk["key"])
                for mk in bk.get("markets", []):
                    if mk["key"] != "h2h":
                        continue
                    for oc in mk["outcomes"]:
                        if oc["name"].lower() == "draw":
                            sel = "draw"
                        elif _canon(oc["name"]) == home:
                            sel = "home"
                        elif _canon(oc["name"]) == away:
                            sel = "away"
                        else:
                            continue
                        prices[sel].append(oc["price"])
            best = {s: _consensus(p) for s, p in prices.items() if p}
            if best:
                matches.append({"home": home, "away": away,
                                "commence": e["commence_time"], "best": best})

        wn = c.get(f"{BASE}/sports/soccer_fifa_world_cup_winner/odds/",
                   params={"apiKey": key, "regions": regions, "markets": "outrights",
                           "oddsFormat": "decimal"})
        wn.raise_for_status()
        remaining = wn.headers.get("x-requests-remaining", remaining)
        champ_prices: dict[str, list] = {}
        for e in wn.json():
            for bk in e.get("bookmakers", []):
                books.add(bk["key"])
                for mk in bk.get("markets", []):
                    for oc in mk["outcomes"]:
                        champ_prices.setdefault(_canon(oc["name"]), []).append(oc["price"])
        champion = {nm: _consensus(p) for nm, p in champ_prices.items() if p}
    return {"fetched_at": now.isoformat(timespec="seconds"), "regions": regions,
            "books": len(books), "remaining": remaining,
            "matches": matches, "champion": champion}


def _load_file() -> dict | None:
    if LIVE_PATH.exists():
        try:
            return json.loads(LIVE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def _load_into_db(con, data: dict) -> int:
    from . import db as dbm
    lookup = {(r["home_team"], r["away_team"]): r["number"] for r in con.execute(
        "SELECT number, home_team, away_team FROM matches WHERE home_team IS NOT NULL "
        "AND away_team IS NOT NULL AND home_score IS NULL")}
    teams = {r["name"] for r in con.execute("SELECT name FROM teams")}
    ts, n = dbm.now(), 0
    for m in data.get("matches", []):
        num = lookup.get((m["home"], m["away"]))
        if not num:
            continue
        for sel, vals in m["best"].items():           # vals = [median, best, n]
            con.execute("INSERT INTO odds (ts, market, selection, decimal_odds) "
                        "VALUES (?,?,?,?)", (ts, f"match:{num}", sel, vals[0]))
            n += 1
    for team, vals in data.get("champion", {}).items():
        if team in teams:
            con.execute("INSERT INTO odds (ts, market, selection, decimal_odds) "
                        "VALUES (?,?,?,?)", (ts, "champion", team, vals[0]))
            n += 1
    con.commit()
    return n


def sync(con, min_interval_h: float = REFRESH_HOURS) -> dict:
    """Refresh the cache if stale and a key is present, then load it into the DB.
    Always safe to call (self-rate-limited); never raises."""
    data = _load_file()
    status = {"live": False, "books": 0, "fetched_at": None, "fetched_now": False,
              "n_odds": 0}
    key = api_key()
    if key and _age_hours((data or {}).get("fetched_at")) >= min_interval_h:
        try:
            data = fetch(key)
            LIVE_PATH.write_text(json.dumps(data), encoding="utf-8")
            status["fetched_now"] = True
            print(f"odds: fetched {len(data['matches'])} matches + "
                  f"{len(data['champion'])} champion prices from {data['books']} "
                  f"books ({data.get('remaining')} credits left)")
        except Exception as e:
            print(f"odds fetch failed: {e} (using cached odds if any)")
    if not data:
        return status
    try:
        status["n_odds"] = _load_into_db(con, data)
    except Exception as e:
        print(f"odds load failed: {e}")
    status.update(live=True, books=data.get("books", 0),
                  fetched_at=data.get("fetched_at"))
    return status
