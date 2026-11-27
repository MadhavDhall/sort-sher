"""Rule-based checks on an email (no LLM): run on every generated draft and again after you edit one.

Flags:
  - length: body outside 90-150 words (links, placeholders and the name line don't count)
  - subject: more than 8 words, or only generic words ("Software Engineering Intern Role")
  - format: no blank line anywhere (one block of text instead of greeting + paragraphs)
  - banned: a phrase from prompts/banned_phrases.txt
  - placeholder: something left unfilled, like [PORTFOLIO], {first_name}, <company> or TODO
  - too_similar: shares too many 4-word phrases with your draft for ANOTHER company (looks templated)
  - (follow-ups only) bump: "just bumping this", "any update?"... a follow-up must add something new
All but too_similar are "hard" problems: draft.py asks the LLM to fix them once.
"""

import re

from .. import outreach_config as cfg
from . import load_prompt

PLACEHOLDER = re.compile(r"\[[A-Z][A-Z_ ]*\]|\{[^{}\s]{1,30}\}|<[a-z_ ]{2,30}>|\bTODO\b|\bXXX\b|\bINSERT\b", re.I)
URL = re.compile(r"https?://\S+|www\.\S+|\b[\w.-]+\.(com|io|dev|ai|app|me|in|org)/\S*", re.I)
WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9'’.+#-]*")


def banned_phrases(file: str = "banned_phrases.txt") -> list[str]:
    lines = load_prompt(file).splitlines()
    return [l.strip().lower() for l in lines if l.strip() and not l.startswith("#")]


def word_count(body: str) -> int:
    """Words in the email itself: URLs and [PLACEHOLDERS] don't count."""
    return len(WORD.findall(PLACEHOLDER.sub(" ", URL.sub(" ", body))))


def shingles(text: str, n: int = 4) -> set[tuple]:
    words = [w.lower() for w in WORD.findall(URL.sub(" ", text))]
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


def similarity(a: str, b: str) -> float:
    """Jaccard overlap of 4-word phrases: 0 = nothing shared, 1 = the same text."""
    sa, sb = shingles(a), shingles(b)
    return len(sa & sb) / len(sa | sb) if sa and sb else 0.0


# A subject made only of these words could be sent to any company.
GENERIC_SUBJECT_WORDS = {
    "software", "engineering", "engineer", "intern", "internship", "internships", "role", "roles", "position",
    "inquiry", "enquiry", "application", "applying", "interest", "interested", "student", "opportunity",
    "about", "for", "in", "the", "a", "an", "at", "re", "and", "your", "my", "team", "job", "query", "request",
    "research", "ai", "ml", "summer", "remote", "junior", "developer", "sde", "swe", "2026", "2027", "2028",
}


def lint(subject: str, body: str, other_drafts: list[tuple[str, str]] = (), followup: bool = False) -> list[str]:
    """other_drafts: (company name, body) of this student's drafts for OTHER companies.
    followup=True (Part 4): a short follow-up in the same thread. Its subject is "Re: <the first email's>",
    so the subject checks are skipped; it has its own length and must not be a plain "bump"."""
    from .. import tracker_config

    flags = []
    words = word_count(body)
    low, high = (tracker_config.FOLLOWUP_MIN_WORDS, tracker_config.FOLLOWUP_MAX_WORDS) if followup \
        else (cfg.MIN_WORDS, cfg.MAX_WORDS)
    if not low <= words <= high:
        flags.append(f"length: {words} words (want {low}-{high})")
    if not followup and len(subject.split()) > cfg.MAX_SUBJECT_WORDS:
        flags.append(f"subject: {len(subject.split())} words (want at most {cfg.MAX_SUBJECT_WORDS})")
    subject_words = {w.lower().strip(".,:-/") for w in WORD.findall(subject)}
    if not followup and subject_words and subject_words <= GENERIC_SUBJECT_WORDS:
        flags.append("subject: generic; name their product/launch or your matching project")
    if "\n\n" not in body.strip():
        flags.append("format: one block of text; use a greeting line and short paragraphs")
    text = f"{subject}\n{body}".lower().replace("’", "'")
    for phrase in banned_phrases() + (banned_phrases("followup_banned_phrases.txt") if followup else []):
        if phrase in text:
            flags.append(f"banned: \"{phrase}\"")
    for m in sorted(set(PLACEHOLDER.findall(f"{subject}\n{body}"))):
        flags.append(f"placeholder: {m} is not filled in")
    for company, other in other_drafts:
        score = similarity(body, other)
        if score > cfg.SIMILARITY_MAX:
            flags.append(f"too_similar: {score:.0%} of phrases shared with your {company} draft")
    return flags


def hard_problems(flags: list[str]) -> list[str]:
    """The flags worth one regeneration (similarity is only shown, the student decides)."""
    return [f for f in flags if not f.startswith("too_similar")]
