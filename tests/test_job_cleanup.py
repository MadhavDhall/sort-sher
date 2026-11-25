"""The shared clean-up rules every JobDraft goes through (models.JobDraft.clean_up).
Cases are taken from real jobs that were shown wrongly in the web app."""

from datetime import datetime, timedelta, timezone

import pytest

from sort_sherr.extractors.ats import ashby
from sort_sherr.models import JobDraft, normalize_employment_type
from sort_sherr.storage import db
from sort_sherr.models import Company


# ---------- remote ----------

@pytest.mark.parametrize("location, expected", [
    ("Remote, EMEA", True),                # was missing from the Remote filter (FirstWork)
    ("Remote Americas + Europe", True),    # River
    ("Remote - UK", True),                 # Oscilar
    ("Paris", None),                       # nothing said -> unknown, not False
    ("Berlin (not remote)", False),
    ("Non-remote, NYC office", False),
])
def test_remote_guessed_from_location_when_unknown(location, expected):
    assert JobDraft(title="Engineer", location=location).remote is expected


def test_source_value_wins_over_guess():
    # an ATS that explicitly says hybrid (False) is not overridden by "Remote" in the text
    assert JobDraft(title="Engineer", location="Remote (Hybrid UK)", remote=False).remote is False


def test_ashby_hybrid_is_not_remote():
    # Real Lucis data: Ashby sends isRemote=True for HYBRID jobs. workplaceType is the truth.
    data = {"jobs": [
        {"title": "Platform engineering", "location": "Paris", "isRemote": True, "workplaceType": "Hybrid"},
        {"title": "BDR", "location": "United States", "isRemote": True, "workplaceType": "Remote"},
        {"title": "Founders associate", "location": "Paris", "isRemote": False, "workplaceType": "OnSite"},
        {"title": "Customer Support", "location": "Montevideo", "isRemote": None, "workplaceType": None},
    ]}
    assert [j.remote for j in ashby.parse(data)] == [False, True, False, None]


# ---------- internships ----------

@pytest.mark.parametrize("title", [
    "Working Student - Heating System", "Werkstudent Marketing", "Software Engineering Trainee",
    "Apprenticeship - Data", "Stagiaire Produit", "Summer Intern 2027",
])
def test_more_internship_words(title):
    assert JobDraft(title=title).is_internship is True


@pytest.mark.parametrize("title", [
    "Senior Director, International Development", "Senior Internal Auditor", "Early-stage GTM Lead",
])
def test_lookalike_words_are_not_internships(title):
    assert JobDraft(title=title).is_internship is not True


# ---------- employment type ----------

@pytest.mark.parametrize("raw, label", [
    ("FullTime", "Full-time"), ("FULL_TIME", "Full-time"), ("full_time", "Full-time"),
    ("Exempt: Full-Time", "Full-time"), ("Full-time (CDI)", "Full-time"), ("Permanent", "Full-time"),
    ("Intern", "Internship"), ("Part-time", "Part-time"), ("Paid, Fractional / Contract", "Part-time"),
    ("Contract, hourly", "Contract"), ("Temporary", "Temporary"),
    ("Justin here, founder @ Diffusion.", None),  # HN text that leaked into the field
    ("W2", None), (None, None),
])
def test_employment_type_normalized(raw, label):
    assert normalize_employment_type(raw) == label


def test_intern_employment_type_marks_internship():
    job = JobDraft(title="HYROX Operations Associate (stage)", employment_type="Intern")
    assert job.employment_type == "Internship" and job.is_internship is True


# ---------- dates + titles ----------

def test_dates_stored_in_utc():
    ny = timezone(timedelta(hours=-4))
    job = JobDraft(title="x", posted_at=datetime(2026, 11, 3, 10, 0, tzinfo=ny))
    assert job.posted_at.tzinfo == timezone.utc and job.posted_at.hour == 14


def test_title_whitespace_collapsed():
    assert JobDraft(title="  AI   Engineer\n ").title == "AI Engineer"


# ---------- duplicates ----------

def test_same_title_and_location_saved_once():
    conn = db.connect(":memory:")
    cid = db.upsert_company(conn, Company(name="Mozilla", domain="mozilla.org", sources=["hn"]))
    jobs = [
        JobDraft(title="Senior Software Engineer, OS Integration", location="Remote Canada", apply_url="https://x/1"),
        JobDraft(title="Senior Software Engineer, OS Integration", location="Remote Canada", apply_url="https://x/2"),
        JobDraft(title="Senior Software Engineer, OS Integration", location="Remote France", apply_url="https://x/3"),
    ]
    assert db.save_jobs(conn, cid, 1, jobs)["new"] == 2
