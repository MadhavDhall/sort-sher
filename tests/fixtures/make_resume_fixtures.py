"""Builds the FAKE resume fixtures (resume_fake.pdf / resume_fake.docx). Made-up person, no real data.

Run once from the repo root:  .venv/bin/python tests/fixtures/make_resume_fixtures.py
The PDF is written by hand (no reportlab needed): one page of Helvetica text + one link annotation,
so we can test that links hidden behind a word ("LinkedIn") are found.
"""

from pathlib import Path

import docx

HERE = Path(__file__).parent
LINES = [
    "Priya Raman",
    "priya.raman@example.com | +91 98765 43210 | github.com/priya-fake | LinkedIn",
    "EDUCATION",
    "Indian Institute of Technology Roorkee - B.Tech, Computer Science, 2022 - 2026",
    "SKILLS",
    "Python, ReactJS, Node, Postgres, PyTorch, LangChain, Docker",
    "PROJECTS",
    "PaperPal - RAG search over 10k arXiv papers with LangChain + FAISS. 300 weekly users. www.paperpal-demo.dev",
    "ShopAPI - REST backend for a campus store in FastAPI + PostgreSQL, 2k orders/month",
    "EXPERIENCE",
    "ML Intern, Acme Analytics (May 2024 - Jul 2024): built churn model in PyTorch",
    "ACHIEVEMENTS",
    "Codeforces Expert; Smart India Hackathon 2023 finalist",
]
LINKEDIN = "https://www.linkedin.com/in/priya-fake"


def make_docx():
    d = docx.Document()
    for line in LINES:
        d.add_paragraph(line)
    # a real hyperlink relationship, like Word makes for "LinkedIn"
    d.part.relate_to(LINKEDIN, docx.opc.constants.RELATIONSHIP_TYPE.HYPERLINK, is_external=True)
    d.save(HERE / "resume_fake.docx")


def make_pdf():
    esc = lambda s: s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")  # noqa: E731
    text = "BT /F1 10 Tf 40 800 Td 14 TL\n" + "\n".join(f"({esc(line)}) '" for line in LINES) + "\nET"
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> "
        "/Contents 5 0 R /Annots [6 0 R] >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(text)} >>\nstream\n{text}\nendstream",
        f"<< /Type /Annot /Subtype /Link /Rect [380 770 430 785] /A << /S /URI /URI ({LINKEDIN}) >> >>",
    ]
    out = "%PDF-1.4\n"
    offsets = []
    for i, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{body}\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n" + "".join(f"{o:010d} 00000 n \n" for o in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    (HERE / "resume_fake.pdf").write_text(out, encoding="latin-1")


if __name__ == "__main__":
    make_docx()
    make_pdf()
    print("wrote resume_fake.docx and resume_fake.pdf")
