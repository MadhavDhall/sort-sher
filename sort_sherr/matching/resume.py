"""Step A: resume file -> CandidateProfile.

Flow:
  PDF/DOCX -> plain text (+ hyperlinks hidden behind words like "GitHub")
           -> sha256 of the FILE -> already parsed? return the saved profile, no LLM call
           -> redact(): name, email, phone and URLs are cut out LOCALLY and replaced by
              placeholders like [EMAIL] and [LINK_1]. Only this redacted text goes to Gemini.
           -> LLM structured output -> ParsedResume
           -> put links back, normalize skill names -> CandidateProfile -> SQLite

Privacy rules (resumes are personal data):
  - never log or print the resume text
  - the Gemini free tier may use prompts to improve Google's products, so personal identifiers
    never leave this computer (see redact())
"""

import hashlib
import json
import re
from pathlib import Path

from ..storage import db
from .models import CandidateProfile, Education, ParsedResume

# ---------- file -> text ----------


def extract_text(path: Path) -> tuple[str, list[str]]:
    """Returns (text, hyperlinks). Hyperlinks matter: many resumes show just the word "GitHub"
    with the real URL hidden in a link annotation."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _pdf_text(path)
    if suffix == ".docx":
        return _docx_text(path)
    raise ValueError(f"unsupported resume type {suffix!r}: use .pdf or .docx")


def _pdf_text(path: Path) -> tuple[str, list[str]]:
    from pypdf import PdfReader

    reader = PdfReader(path)
    pages, links = [], []
    for page in reader.pages:
        pages.append(page.extract_text() or "")
        for annot in page.get("/Annots") or []:
            action = annot.get_object().get("/A") or {}
            uri = action.get("/URI")
            if uri:
                links.append(str(uri))
    return "\n".join(pages), links


def _docx_text(path: Path) -> tuple[str, list[str]]:
    import docx

    document = docx.Document(path)
    lines = [p.text for p in document.paragraphs]
    for table in document.tables:  # many resume templates put everything in tables
        for row in table.rows:
            lines.append(" | ".join(cell.text for cell in row.cells))
    links = [rel.target_ref for rel in document.part.rels.values() if "hyperlink" in rel.reltype]
    return "\n".join(lines), links


def file_hash(path: Path, user_id: int | None = None) -> str:
    """The resume's cache key. On the hosted site the student's id is part of it, so two students
    never share a parsed resume (and one can't find another's by uploading the same file)."""
    return resume_key(path.read_bytes(), user_id)


def resume_key(data: bytes, user_id: int | None = None) -> str:
    prefix = f"user{user_id}:".encode() if user_id is not None else b""  # None = local use: plain hash
    return hashlib.sha256(prefix + data).hexdigest()


# ---------- redaction (runs locally, before anything is sent to the LLM) ----------

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
URL = re.compile(
    r"(https?://\S+|www\.\S+|\b(?:github\.com|linkedin\.com|gitlab\.com|leetcode\.com|codeforces\.com|kaggle\.com)/\S+)",
    re.I,
)
PHONE_CANDIDATE = re.compile(r"\+?\d[\d\s().-]{8,16}\d")
LONG_ID = re.compile(r"\b\d{7,}\b")  # roll / registration / enrollment numbers identify you too


def _is_phone(s: str) -> bool:
    digits = re.sub(r"\D", "", s)
    # 10-13 digits = a phone number. Date ranges like "2022 - 2026" have only 8 digits.
    return 10 <= len(digits) <= 13


def _looks_like_name(line: str) -> bool:
    words = line.split()
    return 1 < len(words) <= 4 and not re.search(r"[\d@/:|,&()]", line)


def find_name(text: str, links: list[str] = ()) -> str | None:
    """GUESS the student's name. Templates put it in different places, so:
    1. a short name-like line with a word that appears inside the email username or a
       LinkedIn/GitHub URL ("Asha Verma" + averma99@... or linkedin.com/in/asha-verma).
       Works wherever the name sits on the page.
    2. otherwise the first line, if it looks like a name (most resumes start with it)
    It's only a guess: `parse-resume` always asks you to confirm it before anything is sent."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    handles = [m.group(0).split("@")[0] for m in EMAIL.finditer(text)]
    handles += [u.rstrip("/").rsplit("/", 1)[-1] for u in [*URL.findall(text), *links] if "/" in u]
    handles = [h.lower() for h in handles]
    for line in lines:
        if _looks_like_name(line) and any(len(w) >= 3 and w.lower() in h for w in line.split() for h in handles):
            return line
    return lines[0] if lines and _looks_like_name(lines[0]) else None


def guess_name(path: Path) -> str | None:
    """find_name() straight from a file (used by the CLI to pre-fill the question)."""
    text, links = extract_text(path)
    return find_name(text, links)


def grad_year(profile: CandidateProfile) -> int | None:
    """The 4-digit year in the first education entry's grad_date, if any."""
    for e in profile.education:
        m = re.search(r"\b(19|20)\d{2}\b", e.grad_date or "")
        if m:
            return int(m.group(0))
    return None


def set_grad_year(profile: CandidateProfile, year: int) -> CandidateProfile:
    """The student knows their passing-out year better than the LLM (resumes often don't state it)."""
    if not profile.education:
        profile.education.append(Education())
    profile.education[0].grad_date = str(year)
    return profile


def redact(text: str, name: str | None = None) -> tuple[str, dict]:
    """Cut out personal identifiers. Returns (redacted text, {"name", "emails", "phones", "links"}).
    `name`: give it when you know it; otherwise find_name() guesses."""
    pii: dict = {"name": name or find_name(text), "emails": [], "phones": [], "links": []}

    def link(m):
        url = m.group(0).rstrip(".,;)")
        if url not in pii["links"]:
            pii["links"].append(url)
        return f"[LINK_{pii['links'].index(url) + 1}]"

    def email(m):
        pii["emails"].append(m.group(0))
        return "[EMAIL]"

    def phone(m):
        if not _is_phone(m.group(0)):
            return m.group(0)
        pii["phones"].append(m.group(0).strip())
        return "[PHONE]"

    text = EMAIL.sub(email, text)  # emails first, so "a@github.com" isn't treated as a URL
    text = URL.sub(link, text)
    text = PHONE_CANDIDATE.sub(phone, text)
    text = LONG_ID.sub("[ID]", text)
    if pii["name"]:
        # also remove the name if it appears again (e.g. in a footer); first and last name alike
        for part in [pii["name"], *pii["name"].split()]:
            if len(part) > 2:
                text = re.sub(rf"\b{re.escape(part)}\b", "[NAME]", text, flags=re.I)
    return text, pii


# ---------- skill names ----------

# Lowercased alias -> one canonical name. Short and boring on purpose: add entries when you see them.
SKILL_ALIASES = {
    "reactjs": "React", "react.js": "React", "react js": "React",
    "nodejs": "Node.js", "node": "Node.js", "node js": "Node.js",
    "nextjs": "Next.js", "next": "Next.js",
    "vuejs": "Vue", "vue.js": "Vue",
    "expressjs": "Express", "express.js": "Express",
    "js": "JavaScript", "javascript": "JavaScript", "ts": "TypeScript", "typescript": "TypeScript",
    "py": "Python", "python3": "Python",
    "c++": "C++", "cpp": "C++", "golang": "Go",
    "postgres": "PostgreSQL", "postgresql": "PostgreSQL", "mongo": "MongoDB", "mongodb": "MongoDB",
    "mysql": "MySQL", "sql": "SQL",
    "tensorflow": "TensorFlow", "tf": "TensorFlow", "pytorch": "PyTorch", "torch": "PyTorch",
    "sklearn": "scikit-learn", "scikit learn": "scikit-learn",
    "ml": "Machine Learning", "machine learning": "Machine Learning",
    "dl": "Deep Learning", "deep learning": "Deep Learning",
    "nlp": "NLP", "llm": "LLMs", "llms": "LLMs", "genai": "GenAI", "generative ai": "GenAI",
    "langchain": "LangChain", "huggingface": "Hugging Face", "hugging face": "Hugging Face",
    "aws": "AWS", "gcp": "GCP", "google cloud": "GCP", "k8s": "Kubernetes", "kubernetes": "Kubernetes",
    "docker": "Docker", "git": "Git", "github": "Git",
    "fastapi": "FastAPI", "django": "Django", "flask": "Flask",
    "tailwind": "Tailwind CSS", "tailwindcss": "Tailwind CSS",
    "rest": "REST APIs", "rest api": "REST APIs", "rest apis": "REST APIs",
}


def normalize_skill(skill: str) -> str:
    s = " ".join(skill.split())
    return SKILL_ALIASES.get(s.lower(), s)


def normalize_skills(skills: list[str]) -> list[str]:
    """Normalize and dedupe, keeping the original order."""
    out: list[str] = []
    for s in skills:
        n = normalize_skill(s)
        if n and n.lower() not in {o.lower() for o in out}:
            out.append(n)
    return out


# ---------- LLM ----------

PROMPT = """Extract a structured profile from this student's resume.
Personal details were replaced by placeholders like [NAME], [EMAIL], [PHONE], [LINK_1]. Keep link
placeholders exactly as written when they belong to a project.
Rules:
- Only use what the resume says. Never invent skills, numbers or projects.
- Skills: canonical names ("React" not "ReactJS", "Node.js" not "Node", "PostgreSQL" not "Postgres").
  Include tools/languages used in projects and experience too.
- focus_areas: 2-4 short labels inferred from the projects and experience, chosen from:
  backend, frontend, fullstack, mobile, ML, GenAI, data, devops/infra, systems, security, robotics,
  design, product, research. Strongest first.
- grad_date: only if the resume clearly states the expected graduation; ignore years inside
  registration/roll numbers ([ID]/2027 is NOT a graduation date). Otherwise null.
- Use null / empty lists for anything not stated.

Resume:
{text}"""


def build_profile(parsed: ParsedResume, pii: dict) -> CandidateProfile:
    """Put the locally kept personal bits back and clean up skill names."""
    links = pii["links"]

    def unplace(s: str) -> str:
        m = re.fullmatch(r"\[LINK_(\d+)\]", s.strip())
        return links[int(m.group(1)) - 1] if m and int(m.group(1)) <= len(links) else s

    profile = CandidateProfile(**parsed.model_dump(), name=pii["name"], links=links)
    profile.skills = normalize_skills(profile.skills)
    for p in profile.projects:
        p.tech_stack = normalize_skills(p.tech_stack)
        p.links = [unplace(x) for x in p.links]
    for e in profile.experience:
        e.tech_stack = normalize_skills(e.tech_stack)
    return profile


async def parse_resume(conn, provider, path: Path, name: str | None = None,
                       user_id: int | None = None) -> tuple[int, CandidateProfile, bool]:
    """Returns (candidate_id, profile, was_cached). user_id: the student it belongs to (hosted site)."""
    resume_hash = file_hash(path, user_id)
    saved = db.get_candidate_by_hash(conn, resume_hash)
    if saved:
        return saved["id"], CandidateProfile.model_validate_json(saved["profile_json"]), True

    text, hidden_links = extract_text(path)
    if len(text.strip()) < 100:
        raise ValueError("could not read text from this file (is it a scanned image? try exporting a text PDF)")
    if not name:
        name = find_name(text, hidden_links)
    redacted, pii = redact(text, name)
    if not pii["name"]:
        # Safer to stop than to send a resume with the name still in it.
        raise ValueError('could not find your name to remove it; pass it with --name "First Last"')
    for url in hidden_links:  # links behind words like "GitHub": kept locally, never sent
        if url not in pii["links"] and not url.startswith("mailto:"):
            pii["links"].append(url)

    parsed, _usage = await provider.generate(PROMPT.format(text=redacted), ParsedResume)
    profile = build_profile(parsed, pii)
    model = getattr(provider, "model", "unknown")
    candidate_id = db.save_candidate(conn, resume_hash, profile.model_dump_json(), json.dumps(pii), model, user_id)
    return candidate_id, profile, False
