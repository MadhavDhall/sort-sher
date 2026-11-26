"""Suggest which roles a student should target, from their parsed profile. No LLM: a fixed,
readable mapping from focus areas (and a few skills) to the role choices `match --role` accepts."""

from .models import CandidateProfile

FOCUS_TO_ROLES = {
    "backend": ["backend", "sde"],
    "frontend": ["frontend", "sde"],
    "fullstack": ["fullstack", "sde"],
    "mobile": ["mobile", "sde"],
    "ml": ["ai-ml"],
    "genai": ["ai-ml"],
    "research": ["ai-ml"],
    "robotics": ["ai-ml"],
    "data": ["data", "ai-ml"],
    "devops/infra": ["devops", "backend"],
    "systems": ["backend", "sde"],
    "security": ["sde"],
    "design": ["design"],
    "product": ["product"],
}
# A skill on its own also hints at a role (in case focus_areas came back empty).
SKILL_TO_ROLE = {
    "React": "frontend", "Next.js": "fullstack", "Node.js": "backend", "FastAPI": "backend", "Django": "backend",
    "PyTorch": "ai-ml", "TensorFlow": "ai-ml", "LangChain": "ai-ml", "LLMs": "ai-ml", "Machine Learning": "ai-ml",
    "Kubernetes": "devops", "Docker": "devops", "Flutter": "mobile", "Swift": "mobile", "Kotlin": "mobile",
}


def suggest_roles(profile: CandidateProfile, max_roles: int = 4) -> list[tuple[str, str]]:
    """[(role, why), ...] strongest first, e.g. [("backend", "focus area: backend"), ...]."""
    out: dict[str, str] = {}
    for focus in profile.focus_areas:
        for role in FOCUS_TO_ROLES.get(focus.lower(), []):
            out.setdefault(role, f"focus area: {focus}")
    for skill in profile.skills:
        role = SKILL_TO_ROLE.get(skill)
        if role:
            out.setdefault(role, f"skill: {skill}")
    return list(out.items())[:max_roles]
