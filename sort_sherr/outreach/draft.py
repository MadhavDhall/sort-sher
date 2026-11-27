"""Step C: two email drafts per company, checked before the student ever sees them.

Flow for one company:
  inputs: student items (S1..) + brief facts (F1..) + Part 2 match reasons + the ask
  -> LLM writes variant A (product-led) and B (project-led), citing the ids it used
  -> fill [NAME], [GITHUB], ... LOCALLY (the LLM never sees the student's name or links)
  -> checks:  rules  (cited ids exist, at least one company fact, every number is in a source)
              LLM    (grounding_check.md: each claim -> the fact/item that backs it)
              lint   (length, subject, banned phrases, unfilled placeholders)
  -> a variant with problems is rewritten ONCE with the problems listed; still bad -> "needs_review"

Nothing here sends email. Drafts are saved; `review` lets the student pick, edit and export one.
"""

import re

from . import fill, load_prompt
from .lint import hard_problems, lint
from .models import DraftPair, GroundingReport, numbered_facts

STYLES = {"A": "product-led", "B": "project-led"}
NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


# ---------- inputs ----------


def student_items(profile) -> list[dict]:
    """The resume as numbered items. No name, no links: those stay on this computer."""
    items = []

    def add(text):
        items.append({"id": f"S{len(items) + 1}", "text": " ".join(text.split())})

    for e in profile.education:
        parts = [e.degree, e.branch, e.college, e.year and f"year {e.year}", e.grad_date and f"graduating {e.grad_date}"]
        if any(parts):
            add("Education: " + ", ".join(str(x) for x in parts if x))
    for p in profile.projects:
        add(f"Project \"{p.title}\": {p.description or ''} Tech: {', '.join(p.tech_stack) or 'n/a'}."
            + (f" Result: {p.impact}." if p.impact else ""))
    for x in profile.experience:
        add(f"Experience: {x.role} at {x.organization or '?'} ({x.start or '?'} - {x.end or '?'}). "
            f"{x.description or ''} Tech: {', '.join(x.tech_stack) or 'n/a'}.")
    if profile.skills:
        add("Skills: " + ", ".join(profile.skills))
    for a in profile.achievements[:3]:
        add(f"Achievement: {a}")
    return items


def link_values(profile, pii: dict) -> dict[str, str]:
    """Placeholder -> real value, filled in locally after the LLM call.
    Links come from the resume, or from `draft --github/--linkedin/--portfolio` (saved in pii_json)."""
    values = {"[NAME]": pii.get("name") or ""}
    for url in [*profile.links, *pii.get("links", [])]:
        low = url.lower()
        if "github.com" in low and "[GITHUB]" not in values:
            values["[GITHUB]"] = url
        elif "linkedin.com" in low and "[LINKEDIN]" not in values:
            values["[LINKEDIN]"] = url
        elif low.startswith("http") and not re.search(r"leetcode|codeforces|codechef|kaggle|mailto", low) \
                and "[PORTFOLIO]" not in values:
            values["[PORTFOLIO]"] = url
    return {k: v for k, v in values.items() if v}


# Models sometimes invent their own spelling of a link placeholder: [GITHUB_LINK], [Resume Link]...
LINK_PLACEHOLDER = re.compile(r"\[\s*(GITHUB|LINKEDIN|PORTFOLIO|RESUME|WEBSITE|CV)[ _-]*(LINK|URL|PROFILE)?\s*\]", re.I)
ALIASES = {"github": "[GITHUB]", "linkedin": "[LINKEDIN]", "portfolio": "[PORTFOLIO]", "website": "[PORTFOLIO]"}


def fill_placeholders(text: str, values: dict[str, str]) -> str:
    """Put the student's real name/links in. A link placeholder we have no value for (e.g. a resume
    link you never gave) is removed with its line, rather than left for you to spot."""
    def link(m):
        return values.get(ALIASES.get(m.group(1).lower(), ""), "")

    text = LINK_PLACEHOLDER.sub(link, text)
    for placeholder, value in values.items():
        text = text.replace(placeholder, value)
    text = text.replace("(no links)", "")
    lines = [l.rstrip() for l in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def match_text(match: dict) -> str:
    lines = []
    if match.get("why_fit"):
        lines.append(f"Why it fits: {match['why_fit']}")
    for e in match.get("reasons") or []:
        lines.append(f"- student: {e['resume_item']}  <->  company: {e['company_fact']}")
    if match.get("job_title"):
        kind = "internship" if match.get("job_is_internship") == 1 else "NOT an internship; don't ask for it"
        lines.append(f"Best open role: {match['job_title']} ({kind})")
    return "\n".join(lines) or "(no reasons saved)"


def facts_text(facts: list[dict]) -> str:
    return "\n".join(f"{f['id']}: {f['text']}" + (f" ({f['date']})" if f.get("date") else "") for f in facts)


def items_text(items: list[dict]) -> str:
    return "\n".join(f"{i['id']}: {i['text']}" for i in items)


# ---------- checks ----------


def rule_problems(v, fact_ids: set, item_ids: set, sources: str) -> list[str]:
    """Checks that don't need an LLM. `v` is the variant BEFORE placeholders are filled."""
    problems = []
    if not v.facts_used:
        problems.append("cites no company fact: the opening must use one of the facts")
    bad = [x for x in v.facts_used if x not in fact_ids] + [x for x in v.student_items_used if x not in item_ids]
    if bad:
        problems.append(f"cites ids that don't exist: {', '.join(bad)}")
    plain_sources = sources.replace(",", "")
    for n in NUMBER.findall(v.subject + " " + v.body):
        if n.replace(",", "") not in plain_sources:
            problems.append(f"the number {n} is not in any fact or resume item")
    return problems


async def grounding_problems(provider, variants: dict, facts: list[dict], items: list[dict], ask: str):
    """One LLM call checks all variants. Returns ({"A": [problems], ...}, usage)."""
    emails = "\n\n".join(f"--- Variant {k} ---\nSubject: {v.subject}\n{v.body}" for k, v in variants.items())
    prompt = fill(load_prompt("grounding_check.md"), facts=facts_text(facts), student=items_text(items),
                  ask=ask, emails=emails)
    report, usage = await provider.generate(prompt, GroundingReport)
    valid = {f["id"] for f in facts} | {i["id"] for i in items}
    problems = {k: [] for k in variants}
    for c in report.claims:
        key = (c.variant or "").strip().upper()[-1:]
        if key in problems and c.about in ("company", "student") and (not c.supported or c.support not in valid):
            problems[key].append(f"unsupported {c.about} claim: \"{c.claim}\"")
    return problems, usage


# ---------- one company ----------


def add_usage(total: dict, used: dict) -> None:
    total["llm_calls"] += 1
    total["prompt_tokens"] += used.get("prompt_tokens", 0)
    total["output_tokens"] += used.get("output_tokens", 0)


async def draft_company(provider, *, profile, pii: dict, company_name: str, brief, match: dict, ask: dict,
                        recipient: dict | None, other_drafts: list[tuple[str, str]]) -> tuple[list[dict], dict]:
    """Returns (two draft dicts ready for db.save_draft minus ids, usage).
    ask: {"role", "window", "setup"}. recipient: the default contact (or None)."""
    usage = {"llm_calls": 0, "prompt_tokens": 0, "output_tokens": 0}
    facts, items = numbered_facts(brief), student_items(profile)
    if not facts:
        raise ValueError(f"the brief for {company_name} has no sourced facts; nothing specific to write about")
    values = link_values(profile, pii)
    have = [p for p in ("[GITHUB]", "[LINKEDIN]", "[PORTFOLIO]") if p in values]
    links = ("then these link placeholders, each on its own line, exactly as written: " + " ".join(have)) if have \
        else "and NO links or link placeholders (the student has none to share)"
    first = (recipient or {}).get("person_name")
    greeting = f"Hi {first.split()[0]}," if first else f"Hi {company_name} team,"
    who = f"{first} ({recipient.get('person_role') or 'role unknown'})" if first else f"the {company_name} team"
    ask_text = f"Role: {ask['role']}. Availability: {ask['window']}. Work setup: {ask['setup']}."
    sources = "\n".join([facts_text(facts), items_text(items), ask_text, match_text(match)])
    fact_ids, item_ids = {f["id"] for f in facts}, {i["id"] for i in items}

    def prompt(feedback: str = "") -> str:
        return fill(load_prompt("draft_email.md"), student=items_text(items), company=company_name,
                    facts=facts_text(facts), match=match_text(match), role=ask["role"], window=ask["window"],
                    setup=ask["setup"], recipient=who, greeting=greeting, links=f"{links}", feedback=feedback)

    def pick(pair: DraftPair) -> dict:
        """Map the LLM's variants to A/B by style (fall back to order)."""
        out = {}
        for v in pair.variants:
            key = "A" if "product" in v.style.lower() else "B" if "project" in v.style.lower() else None
            out.setdefault(key or ("A" if "A" not in out else "B"), v)
        return {k: out[k] for k in ("A", "B") if k in out}

    async def check(variants: dict) -> dict:
        problems = {k: rule_problems(v, fact_ids, item_ids, sources) for k, v in variants.items()}
        llm_problems, used = await grounding_problems(provider, variants, facts, items, ask_text)
        add_usage(usage, used)
        for k, v in variants.items():
            problems[k] += llm_problems.get(k, [])
            problems[k] += hard_problems(lint(v.subject, fill_placeholders(v.body, values)))
        return problems

    pair, used = await provider.generate(prompt(), DraftPair)
    add_usage(usage, used)
    variants = pick(pair)
    problems = await check(variants)
    for k in ("A", "B"):
        if k not in variants:
            problems[k] = ["the LLM did not write this variant"]

    failed = [k for k in ("A", "B") if problems.get(k)]
    if failed:  # one rewrite, with the problems spelled out
        feedback = "\nYOUR PREVIOUS DRAFT HAD THESE PROBLEMS. Fix them and write both variants again:\n" + "\n".join(
            f"- Variant {k}: {p}" for k in failed for p in problems[k])
        pair, used = await provider.generate(prompt(feedback), DraftPair)
        add_usage(usage, used)
        retry = {k: v for k, v in pick(pair).items() if k in failed}
        if retry:
            retry_problems = await check(retry)
            for k, v in retry.items():
                variants[k], problems[k] = v, retry_problems[k]

    by_id = {f["id"]: f for f in facts}
    item_by_id = {i["id"]: i for i in items}
    drafts = []
    for k in ("A", "B"):
        if k not in variants:
            continue
        v = variants[k]
        body = fill_placeholders(v.body, values)
        drafts.append({
            "variant": k, "style": STYLES[k], "subject": v.subject.strip(), "body": body.strip(),
            "facts_used": [by_id[x] for x in v.facts_used if x in by_id],
            "student_items_used": [item_by_id[x] for x in v.student_items_used if x in item_by_id],
            "check_status": "needs_review" if problems.get(k) else "ok",
            "check_notes": problems.get(k, []),
            "lint_flags": lint(v.subject, body, other_drafts),
        })
    return drafts, usage


# ---------- export (CLI file and website download use the same text) ----------


def export_text(company_name: str, recipient: dict | None, subject: str, body: str) -> str:
    """Who to send it to (and the page that address came from), then subject and body."""
    if recipient:
        who = recipient["value"]
        if recipient.get("person_name"):
            who += f"  ({recipient['person_name']}" + (f", {recipient['person_role']}" if recipient.get("person_role") else "") + ")"
        head = f"To: {who}\nFound at: {recipient['source_url']}   <- check this before sending\n"
    else:
        head = "To: (no contact found: look on their website or YC page)\n"
    return f"{head}Subject: {subject}\n\n{body}\n"
