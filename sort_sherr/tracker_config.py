"""Every number that controls Part 4 (the outreach tracker) lives here."""

import os

# ---------- follow-ups (business days = Monday to Friday; holidays are not skipped) ----------
FIRST_FOLLOWUP_DAYS = int(os.getenv("SORT_SHERR_FIRST_FOLLOWUP_DAYS", "5"))  # after the first email, no reply
SECOND_FOLLOWUP_DAYS = int(os.getenv("SORT_SHERR_SECOND_FOLLOWUP_DAYS", "7"))  # after follow-up 1 was SENT
MAX_FOLLOWUPS = 2
GHOST_AFTER_DAYS = 7  # after the last follow-up was sent, still nothing -> ghosted
FOLLOWUP_MIN_WORDS, FOLLOWUP_MAX_WORDS = 25, 60  # a follow-up is short

# ---------- due ----------
INTERVIEW_SOON_DAYS = 3  # interviews this close show up in `due`

# ---------- metrics ----------
SMALL_N = 10  # fewer messages than this in a group: shown, but flagged "too few to judge"
SCORE_BUCKETS = [(70, "70+"), (50, "50-69"), (0, "under 50")]  # Part 2 final_score
TEAM_BUCKETS = [(10, "1-10"), (50, "11-50"), (200, "51-200"), (10**9, "200+")]  # team size, upper bound
