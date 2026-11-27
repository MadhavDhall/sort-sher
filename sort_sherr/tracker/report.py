"""Step D: the outreach report as ONE self-contained HTML file (inline CSS + SVG bars, no JavaScript,
no internet). Built from the database each time; nothing is kept unless you save it with --out."""

from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .. import tracker_config as cfg
from .metrics import pct, summary

TEMPLATES = Path(__file__).parent / "templates"


def render_report(conn, candidate_id: int) -> str:
    env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html"]))
    s = summary(conn, candidate_id)
    replied = dict(s["funnel"])["replied"]
    return env.get_template("report.html").render(
        s=s, cid=candidate_id, replied=replied, pct=pct, small_n=cfg.SMALL_N,
        made=datetime.now().strftime("%Y-%m-%d %H:%M"),
    )
