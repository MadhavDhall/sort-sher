"""All settings in one place. Override with environment variables or a `.env` file."""

import os
from pathlib import Path


def load_dotenv(path: str = ".env") -> None:
    """Read KEY=value lines from .env into the environment (real env variables win).
    A tiny version of the python-dotenv package, so we need one dependency less."""
    if not Path(path).exists():
        return
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_dotenv()

# Database: a local SQLite file by default. Set DATABASE_URL (a postgresql://... URL, e.g. a free Neon
# database) to use Postgres instead: that's what the hosted website and the GitHub refresh job do.
SQLITE_PATH = Path(os.getenv("SORT_SHERR_DB", "data/sort_sherr.db"))
DATABASE_URL = os.getenv("DATABASE_URL") or None
DB_PATH = DATABASE_URL or SQLITE_PATH

# An honest User-Agent: says who we are and where to find out more.
CONTACT = os.getenv("SORT_SHERR_CONTACT", "https://github.com/sort-sherr")
USER_AGENT = f"SortSherr/0.1 (student internship-finder; +{CONTACT})"

# Politeness settings
# robots.txt: off by default (owner's choice: get as much data as possible). We still rate-limit,
# send an honest User-Agent and never touch BLOCKED_DOMAINS. Set SORT_SHERR_RESPECT_ROBOTS=1 to obey it.
RESPECT_ROBOTS = os.getenv("SORT_SHERR_RESPECT_ROBOTS", "0") == "1"
MAX_CONCURRENCY = 10  # requests in flight across all domains
PER_DOMAIN_DELAY = 1.0  # seconds between two requests to the same domain
MAX_RETRIES = 3
TIMEOUT = 20.0  # seconds

# Sites whose terms forbid scraping. We never fetch these.
BLOCKED_DOMAINS = {
    "linkedin.com",
    "wellfound.com",
    "angel.co",
    "indeed.com",
    "glassdoor.com",
    "crunchbase.com",
    # Part 3 (contacts): people-data brokers, and X/Twitter. We may LINK to a public X or LinkedIn
    # profile found on another page, but we never fetch these sites.
    "apollo.io",
    "rocketreach.co",
    "zoominfo.com",
    "x.com",
    "twitter.com",
}

# Web app: if set, the /admin pages ask for this password (any username). Unset = admin is open,
# which is fine on your own laptop but NOT if the site is reachable by others.
ADMIN_PASSWORD = os.getenv("SORT_SHERR_ADMIN_PASSWORD") or None

# Tier 3 (LLM): Google Gemini free tier. Get a free key at https://aistudio.google.com/apikey
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
LLM_MODEL = os.getenv("SORT_SHERR_LLM_MODEL", "gemini-3.5-flash-lite")  # free, fast, good at extraction
LLM_MIN_INTERVAL = float(os.getenv("SORT_SHERR_LLM_MIN_INTERVAL", "6"))  # seconds between calls (~10/min)

# Part 3 (contacts): optional email-finder API. Hunter.io's free plan needs no credit card.
# Unset = we only use public pages and the GitHub API.
HUNTER_API_KEY = os.getenv("HUNTER_API_KEY") or None

# ---------- hosting (student accounts) ----------
# On Render (it sets RENDER=true) every page needs a login. Locally the site stays open and binds to
# 127.0.0.1 only; set SORT_SHERR_LOGIN=1 to try the login pages on your laptop.
HOSTED = bool(os.getenv("RENDER"))
LOGIN_REQUIRED = HOSTED or os.getenv("SORT_SHERR_LOGIN") == "1"
# Signs the login cookie. MUST be set (a long random string) when hosted, or everyone is logged out on
# every restart. Make one with: python -c "import secrets; print(secrets.token_urlsafe(32))"
SESSION_SECRET = os.getenv("SORT_SHERR_SESSION_SECRET") or None
# Per-student limits on the shared free Gemini key (the admin has none)
MAX_RESUMES_PER_USER = int(os.getenv("SORT_SHERR_MAX_RESUMES", "3"))
MAX_MATCH_RUNS_PER_DAY = int(os.getenv("SORT_SHERR_MAX_MATCH_RUNS", "3"))
# Where the admin page links for the data refresh (the GitHub Actions workflow page)
REFRESH_URL = os.getenv("SORT_SHERR_REFRESH_URL") or None
