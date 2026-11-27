"""Every number that controls Part 3 (research, contacts, drafts) lives here, so you can tune it in one place."""

import os
from pathlib import Path

# ---------- research ----------
BRIEF_MAX_AGE_DAYS = int(os.getenv("SORT_SHERR_BRIEF_MAX_AGE_DAYS", "7"))  # older briefs are rebuilt
MAX_SITE_PAGES = 6  # pages from the company's own site (homepage + about/blog/changelog/...)
PAGE_CHARS = 6000  # how much of each page the LLM reads (~1.5k tokens)
MAX_HN_STORIES = 5  # HN stories about the company (Launch HN, Show HN, news links)
MAX_NEWS_ITEMS = 5  # news headlines listed on the YC company page

# ---------- drafts ----------
DAILY_DRAFT_CAP = int(os.getenv("SORT_SHERR_DAILY_DRAFT_CAP", "10"))  # companies drafted per day, per candidate
MIN_WORDS, MAX_WORDS = 90, 150  # email body length
MAX_SUBJECT_WORDS = 8
# Two drafts for DIFFERENT companies sharing more than this share of 4-word phrases look templated.
# (The student's project sentence will repeat a little; a copy-paste email shares far more.)
SIMILARITY_MAX = 0.35

# ---------- where personal files go (both gitignored) ----------
DRAFTS_DIR = Path("drafts")  # approved drafts, one .txt per company
RATINGS_CSV = Path("labels/draft_ratings.csv")  # eval-drafts: judge scores + your own rating
