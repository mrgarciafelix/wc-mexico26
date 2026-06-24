"""Build data/seed/*.json from Wikipedia + Transfermarkt + results.csv.

Run once (results frozen in git); the app's updater only refreshes scores.
"""
from __future__ import annotations

import json
import re
import sys
import time
from io import StringIO
from pathlib import Path

import httpx
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import wiki
from backend.config import (BROWSER_UA, CACHE, RESULTS_CSV_URL, SEED,
                            TM_RANKING_URL, WIKI_KNOCKOUT, WIKI_MAIN,
                            WIKI_SQUADS, canonical)

STAGE_OF = lambda n: ("group" if n <= 72 else "r32" if n <= 88 else
                      "r16" if n <= 96 else "qf" if n <= 100 else
                      "sf" if n <= 102 else "third" if n == 103 else "final")


def cached(name: str, url: str) -> str:
    p = CACHE / f"{name}.html"
    if p.exists():
        return p.read_text(encoding="utf-8")
    html = wiki.fetch(url)
    p.write_text(html, encoding="utf-8")
    return html


def parse_slot(label: str) -> dict:
    """'Winner Group G' / 'Runner-up Group A' / '3rd Group A/E/H/I/J' /
    'Winner Match 73' / 'Loser Match 101' -> structural slot spec."""
    label = label.strip()
    m = re.match(r"^Winners? Group ([A-L])$", label)
    if m:
        return {"type": "W", "group": m.group(1)}
    m = re.match(r"^Runners?-up Group ([A-L])$", label)
    if m:
        return {"type": "RU", "group": m.group(1)}
    m = re.match(r"^3rd Group ([A-L/]+)$", label)
    if m:
        return {"type": "3RD", "from": m.group(1).split("/")}
    m = re.match(r"^Winners? Match (\d+)$", label)
    if m:
        return {"type": "WM", "match": int(m.group(1))}
    m = re.match(r"^Losers? Match (\d+)$", label)
    if m:
        return {"type": "LM", "match": int(m.group(1))}
    return {"type": "TEAM", "team": canonical(label)}


def assign_numbers(matches: list[dict]) -> None:
    """Fill missing match numbers (played fixtures lose them on Wikipedia) by
    elimination inside each section, relying on doc order == number order."""
    by_heading: dict[str, list[dict]] = {}
    for m in matches:
        by_heading.setdefault(m["heading"], []).append(m)
    known_all = {m["match_number"] for m in matches if m["match_number"]}
    for boxes in by_heading.values():
        unknown = [b for b in boxes if not b["match_number"]]
        if not unknown:
            continue
        known = sorted(b["match_number"] for b in boxes if b["match_number"])
        if not known:
            raise RuntimeError(f"no anchors in section {boxes[0]['heading']}")
        lo, hi = min(known), max(known)
        # candidate numbers in this section's range not used anywhere
        pool = [n for n in range(max(1, lo - len(unknown)), hi + len(unknown) + 1)
                if n not in known_all]
        for b in unknown:
            idx = boxes.index(b)
            after = [x["match_number"] for x in boxes[idx + 1:] if x["match_number"]]
            cap = min(after) if after else hi + len(unknown)
            cands = [n for n in pool if n < cap]
            if not cands:
                raise RuntimeError(f"cannot deduce number for {b['home_label']} v {b['away_label']}")
            n = cands[0]
            b["match_number"] = n
            pool.remove(n)
            known_all.add(n)


def fetch_transfermarkt() -> dict[str, dict]:
    out = {}
    with httpx.Client(headers={"User-Agent": BROWSER_UA}, timeout=30,
                      follow_redirects=True) as cl:
        for page in range(1, 10):
            url = TM_RANKING_URL if page == 1 else f"{TM_RANKING_URL}?page={page}"
            p = CACHE / f"tm_{page}.html"
            if p.exists():
                html = p.read_text(encoding="utf-8")
            else:
                r = cl.get(url)
                r.raise_for_status()
                html = r.text
                p.write_text(html, encoding="utf-8")
                time.sleep(1.5)
            for t in pd.read_html(StringIO(html)):
                if "Nation" in t.columns and "Total value" in t.columns:
                    for _, row in t.iterrows():
                        name = canonical(str(row["Nation"]))
                        mv = str(row["Total value"])
                        mm = re.match(r"€([\d.]+)(bn|m|k)", mv)
                        eur = 0.0
                        if mm:
                            mult = {"bn": 1e9, "m": 1e6, "k": 1e3}[mm.group(2)]
                            eur = float(mm.group(1)) * mult
                        out[name] = {
                            "market_value_eur": eur,
                            "fifa_points": float(row.get("Points", 0) or 0),
                            "confed": str(row.get("Confederation", "")),
                        }
    return out


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    main_html = cached("main", WIKI_MAIN)
    ko_html = cached("knockout", WIKI_KNOCKOUT)
    sq_html = cached("squads", WIKI_SQUADS)

    # --- matches ---
    matches = wiki.parse_matches(main_html)
    assert len(matches) == 104, f"expected 104 boxes, got {len(matches)}"
    assign_numbers(matches)
    nums = sorted(m["match_number"] for m in matches)
    assert nums == list(range(1, 105)), f"bad numbers: missing {set(range(1,105)) - set(nums)}"

    groups = wiki.parse_groups(main_html)
    assert sorted(groups) == list("ABCDEFGHIJKL"), f"groups: {sorted(groups)}"
    team_group = {row["team"]: g for g, rows in groups.items() for row in rows}
    teams = sorted(team_group)
    assert len(teams) == 48, f"{len(teams)} teams"

    seed_matches = []
    for m in sorted(matches, key=lambda x: x["match_number"]):
        n = m["match_number"]
        stage = STAGE_OF(n)
        entry = {
            "number": n, "stage": stage,
            "kickoff_utc": m["kickoff_utc"], "stadium": m["stadium"],
            "city": m["city"],
            "home_score": m["home_score"], "away_score": m["away_score"],
            "pen_home": m["pen_home"], "pen_away": m["pen_away"],
        }
        if stage == "group":
            h, a = canonical(m["home_label"]), canonical(m["away_label"])
            assert h in team_group and a in team_group, f"M{n}: {h} v {a}"
            entry.update(group=team_group[h], home_team=h, away_team=a)
            assert team_group[h] == team_group[a]
        else:
            entry.update(home_slot=parse_slot(m["home_label"]),
                         away_slot=parse_slot(m["away_label"]))
        seed_matches.append(entry)

    # --- third place allocation ---
    alloc = wiki.parse_third_place_allocation(ko_html)

    # --- squads ---
    squads = wiki.parse_squads(sq_html)
    missing = [t for t in teams if t not in squads]
    assert not missing, f"squads missing: {missing}"

    # --- transfermarkt ---
    tm = fetch_transfermarkt()
    missing_mv = [t for t in teams if t not in tm]
    assert not missing_mv, f"market values missing: {missing_mv}"

    # --- verify against historical dataset names ---
    hist = pd.read_csv(CACHE / "results.csv")
    hist_teams = set(hist["home_team"]) | set(hist["away_team"])
    missing_hist = [t for t in teams if t not in hist_teams]
    assert not missing_hist, f"not in results.csv: {missing_hist}"

    seed_teams = [{
        "name": t, "group": team_group[t], **tm[t],
    } for t in teams]

    (SEED / "teams.json").write_text(json.dumps(seed_teams, indent=1), encoding="utf-8")
    (SEED / "matches.json").write_text(json.dumps(seed_matches, indent=1), encoding="utf-8")
    (SEED / "third_place_alloc.json").write_text(json.dumps(alloc), encoding="utf-8")
    (SEED / "squads.json").write_text(json.dumps(squads, indent=1), encoding="utf-8")
    played = [m for m in seed_matches if m["home_score"] is not None]
    print(f"OK: 48 teams, 104 matches ({len(played)} played), "
          f"{len(alloc)} alloc combos, {len(squads)} squads")
    for m in played:
        print(f"  played M{m['number']}: {m.get('home_team')} {m['home_score']}-"
              f"{m['away_score']} {m.get('away_team')}")


if __name__ == "__main__":
    main()
