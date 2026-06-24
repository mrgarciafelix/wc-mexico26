"""Quick exploration: download key Wikipedia pages and report table structure.

Saves raw HTML into data/cache/ so later parsing doesn't re-hit the network.
"""
import sys
from pathlib import Path

import httpx
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "data" / "cache"
CACHE.mkdir(parents=True, exist_ok=True)

HEADERS = {"User-Agent": "WCMexico26/0.1 (miguelramongarciafelix@gmail.com) httpx"}

REST = "https://en.wikipedia.org/api/rest_v1/page/html/"
PAGES = {
    "main": REST + "2026_FIFA_World_Cup",
    "group_stage": REST + "2026_FIFA_World_Cup_group_stage",
    "knockout": REST + "2026_FIFA_World_Cup_knockout_stage",
    "squads": REST + "2026_FIFA_World_Cup_squads",
}


def fetch(name: str, url: str) -> str:
    path = CACHE / f"{name}.html"
    if path.exists():
        return path.read_text(encoding="utf-8")
    r = httpx.get(url, headers=HEADERS, follow_redirects=True, timeout=60)
    r.raise_for_status()
    path.write_text(r.text, encoding="utf-8")
    return r.text


def main():
    for name, url in PAGES.items():
        try:
            html = fetch(name, url)
        except Exception as e:
            print(f"== {name}: FETCH FAILED: {e}")
            continue
        print(f"== {name}: {len(html)} bytes")
        try:
            from io import StringIO
            tables = pd.read_html(StringIO(html))
        except ValueError:
            print("   no tables")
            continue
        print(f"   {len(tables)} tables")
        for i, t in enumerate(tables):
            cols = [str(c)[:30] for c in list(t.columns)[:8]]
            print(f"   [{i}] {t.shape}  cols={cols}")
            if i > 80:
                print("   ... (truncated)")
                break


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
