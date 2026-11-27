"""Part 3: company research, contacts and email DRAFTS. Nothing in here sends email.

Prompts live as plain text files in prompts/ (not inline in code), so you can read and tune them
without touching Python. Placeholders look like {{name}}; single braces such as {first} are left alone.
"""

from pathlib import Path

PROMPTS_DIR = Path(__file__).parent / "prompts"


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text()


def fill(template: str, **values) -> str:
    for key, value in values.items():
        template = template.replace("{{" + key + "}}", str(value))
    return template
