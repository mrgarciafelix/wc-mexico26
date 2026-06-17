"""Scrape the latest results, recompute the model, and export the static
snapshot in one shot. Used by publish.ps1 (and reusable by a scheduled CI job).

    .venv\\Scripts\\python.exe -m scripts.refresh_and_export
"""
from __future__ import annotations

from backend import db as dbm
from backend import updater
from scripts.export_static import main as export_snapshot


def main() -> None:
    con = dbm.connect()
    try:
        dbm.init_db(con)
        res = updater.run_update(con, trigger="publish", sync_wiki=True)
        changes = res.get("changes", []) if isinstance(res, dict) else []
        print(f"update: run {res.get('run_id')} · {len(changes)} changes · "
              f"{res.get('n_sims')} sims")
    except Exception as e:                      # keep going on a scrape failure
        print(f"update warning: {e} (exporting last good state)")
    finally:
        con.close()
    export_snapshot()


if __name__ == "__main__":
    main()
