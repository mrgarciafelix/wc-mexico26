"""Current club form for World Cup squad players, folded into team strength.

Pulls each player's current-season club xG + xA + minutes (Understat, top-5
European leagues — where most impactful players are) and turns it into a small,
**bounded** "are the key players firing and fit right now" adjustment. Kept
deliberately small (±~22 Elo) vs the proven Elo+market-value base so an
unvalidated feature can refine but never dominate. Cached to data/player_form.json.

Coverage outside the top-5 leagues falls back to neutral (0). Validation is
forward-looking (the prediction log), since we can't cleanly backtest club form
against historical international dates yet.
"""
from __future__ import annotations

import json
import time
import unicodedata

from .config import DATA

CACHE = DATA / "player_form.json"
LEAGUES = ["ENG-Premier League", "ESP-La Liga", "ITA-Serie A",
           "GER-Bundesliga", "FRA-Ligue 1"]
SEASON = "2024"
REFRESH_DAYS = 3.0
CLUB_FORM_CAP = 22.0       # max Elo swing from club form
MIN_MINUTES = 450          # ignore tiny samples
MIN_COVERAGE = 3           # need >=3 matched players or stay neutral


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
    return " ".join("".join(c for c in s.lower() if c.isalnum() or c == " ").split())


def fetch(season: str = SEASON) -> dict:
    import logging
    import warnings
    logging.disable(logging.INFO)
    warnings.filterwarnings("ignore")
    import soccerdata as sd
    players = []
    for lg in LEAGUES:
        try:
            df = sd.Understat(leagues=lg, seasons=season).read_player_season_stats().reset_index()
            for _, r in df.iterrows():
                players.append({
                    "name": str(r.get("player", "")), "team": str(r.get("team", "")),
                    "min": int(r.get("minutes", 0) or 0),
                    "xg": float(r.get("xg", 0) or 0), "xa": float(r.get("xa", 0) or 0),
                    "shots": int(r.get("shots", 0) or 0)})
        except Exception as e:
            print(f"playerform: {lg} failed: {e}")
    return {"fetched_at": time.time(), "season": season, "players": players}


def _load() -> dict | None:
    if CACHE.exists():
        try:
            return json.loads(CACHE.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def sync(max_age_days: float = REFRESH_DAYS) -> dict:
    data = _load()
    if not data or (time.time() - data.get("fetched_at", 0)) / 86400 >= max_age_days:
        try:
            data = fetch()
            CACHE.write_text(json.dumps(data), encoding="utf-8")
        except Exception as e:
            print(f"playerform fetch failed: {e}")
    return data or {"players": []}


def shots_per90_lookup() -> dict[str, float]:
    """{normalized player name: shots per 90} from the cached Understat feed —
    real shot VOLUME, used to price shot props instead of deriving them from
    goals (which understates high-volume, low-conversion shooters like Vinícius).
    Covers top-5-league players; others fall back to the goal-derived estimate."""
    data = _load() or {}
    out: dict[str, float] = {}
    for p in data.get("players", []):
        mins = p.get("min", 0)
        if mins < MIN_MINUTES or not p.get("shots"):
            continue
        out[_norm(p["name"])] = p["shots"] * 90.0 / mins
    return out


def team_club_form(con) -> dict[str, float]:
    """{team: bounded Elo adjustment} from current club xG/xA + minutes."""
    import numpy as np
    data = sync()
    lookup: dict[str, tuple[float, int]] = {}
    for p in data.get("players", []):
        if p["min"] < MIN_MINUTES:
            continue
        per90 = 90.0 / p["min"]
        lookup[_norm(p["name"])] = ((p["xg"] + p["xa"]) * per90, p["min"])

    teams = [r["name"] for r in con.execute("SELECT name FROM teams")]
    raw: dict[str, float | None] = {}
    cov: dict[str, int] = {}
    for team in teams:
        num = den = 0.0
        n = 0
        for pl in con.execute("SELECT name, importance FROM players WHERE team=?",
                              (team,)):
            hit = lookup.get(_norm(pl["name"]))
            if not hit:
                continue
            prod, mins = hit
            w = pl["importance"] * min(mins / 1500.0, 1.0)
            num += prod * w
            den += w
            n += 1
        cov[team] = n
        raw[team] = (num / den) if den > 0 else None

    vals = np.array([v for v in raw.values() if v is not None])
    mu = float(vals.mean()) if len(vals) else 0.0
    sd = float(vals.std()) if len(vals) > 1 else 1.0
    sd = sd or 1.0
    out: dict[str, float] = {}
    for team in teams:
        if raw[team] is None or cov[team] < MIN_COVERAGE:
            out[team] = 0.0
            continue
        z = (raw[team] - mu) / sd
        conf = min(cov[team] / 8.0, 1.0)        # shrink low-coverage teams
        out[team] = round(float(np.clip(z, -2.2, 2.2) / 2.2 * CLUB_FORM_CAP) * conf, 1)
    out["_coverage"] = cov                       # for diagnostics
    return out
