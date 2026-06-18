"""Central configuration: paths, model constants, name normalization."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Load .env (gitignored) into the environment so local runs pick up secrets
# like WC_ODDS_API_KEY. In CI the same vars come from repo secrets instead.
_envf = ROOT / ".env"
if _envf.exists():
    for _line in _envf.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())
DATA = ROOT / "data"
SEED = DATA / "seed"
CACHE = DATA / "cache"
DB_PATH = DATA / "app.db"
FRONTEND = ROOT / "frontend"

for p in (SEED, CACHE, FRONTEND):
    p.mkdir(parents=True, exist_ok=True)

USER_AGENT = "WCMexico26/0.1 (+https://github.com/mrgarciafelix/wc-mexico26) httpx"
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

WIKI_REST = "https://en.wikipedia.org/api/rest_v1/page/html/"
WIKI_MAIN = WIKI_REST + "2026_FIFA_World_Cup"
WIKI_KNOCKOUT = WIKI_REST + "2026_FIFA_World_Cup_knockout_stage"
WIKI_SQUADS = WIKI_REST + "2026_FIFA_World_Cup_squads"
RESULTS_CSV_URL = (
    "https://raw.githubusercontent.com/martj42/international_results/master/results.csv"
)
TM_RANKING_URL = "https://www.transfermarkt.com/statistik/weltrangliste"

# --- Elo model ---------------------------------------------------------------
ELO_START = 1500.0
ELO_HOME_ADV = 80.0          # Elo points for non-neutral home team
K_BY_TOURNAMENT = {          # match importance -> K factor
    "FIFA World Cup": 60,
    "FIFA World Cup qualification": 40,
    "Friendly": 20,
}
K_CONTINENTAL = 50           # continental finals (Euro, Copa America, AFCON...)
K_DEFAULT = 30               # everything else (Nations League, minor cups)
CONTINENTAL_FINALS = (
    "UEFA Euro", "Copa América", "African Cup of Nations",
    "Africa Cup of Nations", "AFC Asian Cup", "CONCACAF Championship",
    "Gold Cup", "Oceania Nations Cup", "Confederations Cup",
)

# --- Strength blend ----------------------------------------------------------
MV_WEIGHT = 35.0             # Elo points per z-score of log market value
FORM_WEIGHT = 0.5            # multiplier on last-10-match Elo delta, capped
FORM_CAP = 40.0
INJURY_ELO_PER_IMPORTANCE = 60.0   # full-importance player (1.0) out -> -60 Elo
                                   # (~0.25 goals), scaled by player importance
WC_HOST_ELO_BONUS = 80.0     # host playing in own country during the WC
# World Cup matches are tighter/more random than the Elo gap implies (neutral
# venues, elite teams, high stakes). Shrinking the skill gap improves match
# log-loss on BOTH the 2022 WC backtest and the live 2026 games — see
# scripts/evaluate.py. Kept modest (0.88) to avoid overfitting the small samples.
WC_CONFIDENCE = 0.88

# --- Simulation --------------------------------------------------------------
N_SIMS = 50000
ET_LAMBDA_FACTOR = 1 / 3     # extra time goal rate vs 90 minutes
RNG_SEED = None              # None -> fresh randomness each run

# --- Betting -----------------------------------------------------------------
DEFAULT_BANKROLL = 200.0
DEFAULT_KELLY_FRACTION = 0.25

# --- Updater -----------------------------------------------------------------
UPDATE_INTERVAL_MINUTES = 15

# Host city -> country (for home advantage during the tournament)
HOST_CITY_COUNTRY = {
    "Mexico City": "Mexico", "Guadalajara": "Mexico", "Monterrey": "Mexico",
    "Toronto": "Canada", "Vancouver": "Canada",
    "Atlanta": "United States", "Boston": "United States",
    "Dallas": "United States", "Houston": "United States",
    "Kansas City": "United States", "Los Angeles": "United States",
    "Miami": "United States", "New York": "United States",
    "New Jersey": "United States", "East Rutherford": "United States",
    "Philadelphia": "United States", "San Francisco": "United States",
    "Santa Clara": "United States", "Seattle": "United States",
    "Arlington": "United States", "Foxborough": "United States",
    "Inglewood": "United States", "Miami Gardens": "United States",
}

# Aliases: source-specific name -> canonical (Wikipedia) name
TEAM_ALIASES = {
    # martj42 dataset and Transfermarkt variants
    "USA": "United States",
    "Korea Republic": "South Korea",
    "South Korea": "South Korea",
    "Korea, South": "South Korea",
    "IR Iran": "Iran",
    "Côte d'Ivoire": "Ivory Coast",
    "Cote d'Ivoire": "Ivory Coast",
    "Czechia": "Czech Republic",
    "Türkiye": "Turkey",
    "Turkiye": "Turkey",
    "Bosnia": "Bosnia and Herzegovina",
    "Curacao": "Curaçao",
    "Cabo Verde": "Cape Verde",
    "Bosnia-Herzegovina": "Bosnia and Herzegovina",
    "New Zealand ": "New Zealand",
}


def canonical(name: str) -> str:
    name = " ".join(str(name).replace("\xa0", " ").split())
    # strip Wikipedia annotations like "(H)" for hosts
    for suffix in (" (H)", "(H)"):
        if name.endswith(suffix):
            name = name[: -len(suffix)].strip()
    return TEAM_ALIASES.get(name, name)
