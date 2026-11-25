import pytest

from sort_sherr.discovery.ats import detect_ats
from sort_sherr.discovery.careers import find_careers_link
from sort_sherr.models import AtsType


@pytest.mark.parametrize(
    "html, expected",
    [
        ('<a href="https://jobs.ashbyhq.com/solidroad">Careers</a>', (AtsType.ashby, "solidroad")),
        ('<a href="https://jobs.ashbyhq.com/mem0/5fdf00fb-0046">Role</a>', (AtsType.ashby, "mem0")),
        ('<a href="https://boards.greenhouse.io/stripe">Jobs</a>', (AtsType.greenhouse, "stripe")),
        ('<a href="https://job-boards.greenhouse.io/acme/jobs/123">x</a>', (AtsType.greenhouse, "acme")),
        ('<script src="https://boards.greenhouse.io/embed/job_board/js?for=acme"></script>', (AtsType.greenhouse, "acme")),
        ('<iframe src="https://boards.greenhouse.io/embed/job_board?for=acme"></iframe>', (AtsType.greenhouse, "acme")),
        ('<a href="https://jobs.lever.co/spotify/2a55d6c9">x</a>', (AtsType.lever, "spotify")),
        ('<a href="https://apply.workable.com/huggingface/">x</a>', (AtsType.workable, "huggingface")),
        ('<a href="https://ats.rippling.com/saturn/jobs/401593ba">x</a>', (AtsType.rippling, "saturn")),
        ('<a href="https://ats.rippling.com/en-GB/saturn/jobs/401593ba">x</a>', (AtsType.rippling, "saturn")),
        ('<a href="https://app.dover.com/jobs/mixrank">Open Roles</a>', (AtsType.dover, "mixrank")),
        ('<a href="https://jobs.dover.com/mixrank/09a901ed">x</a>', (AtsType.dover, "mixrank")),
        ('<a href="https://jobs.gem.com/rootly">Open roles</a>', (AtsType.gem, "rootly")),
        ("<p>no ats here</p>", None),
    ],
)
def test_detect_ats(html, expected):
    assert detect_ats(html) == expected


def test_detect_ats_from_redirect_url():
    assert detect_ats("<html></html>", url="https://jobs.lever.co/acme") == (AtsType.lever, "acme")


def test_most_mentioned_board_wins():
    html = (
        '<a href="https://jobs.lever.co/partner">partner</a>'
        '<a href="https://jobs.ashbyhq.com/us/1">a</a><a href="https://jobs.ashbyhq.com/us/2">b</a>'
    )
    assert detect_ats(html) == (AtsType.ashby, "us")


def test_find_careers_link_prefers_ats_and_skips_linkedin():
    html = """
      <a href="https://www.linkedin.com/company/acme/jobs">LinkedIn jobs</a>
      <a href="/about">About</a>
      <a href="./careers">Careers</a>
    """
    assert find_careers_link(html, "https://acme.com/") == "https://acme.com/careers"

    html_with_ats = html + '<a href="https://jobs.ashbyhq.com/acme">Open roles</a>'
    assert find_careers_link(html_with_ats, "https://acme.com/") == "https://jobs.ashbyhq.com/acme"


def test_find_careers_link_by_text():
    html = '<a href="/team">Join us</a><a href="/blog">Blog</a>'
    assert find_careers_link(html, "https://acme.com") == "https://acme.com/team"
