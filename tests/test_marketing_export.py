from __future__ import annotations

import csv
import io
import re
import zipfile
from datetime import UTC, datetime, timedelta

from aambridge.dashboard.marketing import (
    FORBIDDEN_COLUMNS,
    MARKETING_FILES,
    PULSE_COLUMNS,
    POST_THEME_LABELS,
    export_marketing_pack,
    marketing_pack_zip,
    marketing_report_pdf,
    marketing_tables,
)
from aambridge.dashboard.report import REPORT_NAME
from aambridge.dashboard.queries import (
    MARKET_INSIGHT_EXCLUDED_FACTORS,
    PipelineFilters,
    market_insight,
    pipeline_rows,
    rolling_hiring,
    rolling_industry_chart,
)
from aambridge.db.models import Company, Job
from aambridge.db.storage import ScanStore
from aambridge.extract.taxonomies import load_yaml
from sqlalchemy import select

from tests.test_dashboard import _database, _job_payload, _seed_pipeline


def _csv_rows(path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _pdf_text(blob: bytes) -> str:
    parts = re.findall(rb"\((?:\\.|[^\\)])*\)\s*Tj", blob)
    decoded = []
    for part in parts:
        inner = part[1:-1]
        inner = re.sub(rb"\)\s*Tj$", b"", inner)
        inner = inner.replace(b"\\(", b"(").replace(b"\\)", b")")
        decoded.append(inner.decode("latin-1", errors="ignore"))
    return " ".join(decoded).casefold()


def test_marketing_pack_has_expected_files_and_no_pii(tmp_path) -> None:
    _engine, sessions = _database(tmp_path)
    _seed_pipeline(sessions)
    output = tmp_path / "marketing"
    written = export_marketing_pack(sessions, output)
    names = {path.name for path in written}
    assert names == {f"{name}.csv" for name in MARKETING_FILES} | {REPORT_NAME}

    dumped: list[str] = []
    for path in written:
        if path.suffix.lower() == ".pdf":
            continue
        rows = _csv_rows(path)
        columns = set(rows[0]) if rows else set(
            next(csv.reader(path.open(encoding="utf-8-sig")))
        )
        assert not (columns & FORBIDDEN_COLUMNS)
        assert "aambridge_fit" not in columns
        dumped.extend(columns)
        dumped.extend(value for row in rows for value in row.values())
    text = " ".join(dumped).casefold()
    assert "aisha" not in text
    assert "rahman" not in text
    assert "@" not in text
    assert "author" not in text
    assert "phone" not in text
    assert "email" not in text


def test_marketing_industry_counts_match_rolling_hiring(tmp_path) -> None:
    _engine, sessions = _database(tmp_path)
    _seed_pipeline(sessions)
    now = datetime.now(UTC)
    facts = rolling_hiring(sessions, days=30, hide_agencies=True, now=now)
    chart = {
        item["industry"]: item
        for item in rolling_industry_chart(facts)["rankings"]
    }
    tables = marketing_tables(sessions, hide_agencies=True, now=now)
    by_industry = {row["industry"]: row for row in tables["marketing_industry_30d"]}
    assert set(by_industry) == set(chart)
    assert "healthcare" in by_industry
    assert by_industry["healthcare"]["job_count"] == chart["healthcare"]["job_count"]
    assert by_industry["healthcare"]["employer_count"] == chart["healthcare"]["company_count"]
    assert by_industry["healthcare"]["job_count"] == 1
    assert by_industry["healthcare"]["employer_count"] == 1

    locations = tables["marketing_location_30d"]
    dubai = next(row for row in locations if row["city"] == "Dubai")
    assert dubai["job_count"] == 1
    assert dubai["country"] == "United Arab Emirates"

    roles = tables["marketing_roles_30d"]
    assert roles[0]["job_title"] == "Healthcare Operations Manager"
    assert roles[0]["count"] == 1
    assert roles[0]["top_industry"] == "healthcare"

    highlights = tables["marketing_employer_highlights"]
    assert [row["company"] for row in highlights] == ["Client Hospital"]
    assert "Aisha" not in highlights[0]["roles_sample"]
    assert highlights[0]["active_jobs"] == 1
    assert "avg_market_insight" not in highlights[0]
    assert "market_insight" not in highlights[0]
    assert "lead_score" not in highlights[0]
    assert "aambridge_fit" not in highlights[0]

    pulse = tables["marketing_weekly_pulse"][0]
    assert "jobs_15d" in PULSE_COLUMNS
    assert pulse["jobs_15d"] == 1
    assert pulse["employers_15d"] == 1
    assert pulse["jobs_30d"] == 1
    assert pulse["employers_30d"] == 1
    assert pulse["leading_industry"] == "healthcare"
    assert pulse["leading_city"] == "Dubai"
    assert pulse["posts_expansion"] == 1
    assert pulse["new_employers"] >= 1

    themes = {(row["kind"], row["name"]): row["count"] for row in tables["marketing_themes_30d"]}
    assert themes[("post_category", "expansion")] == 1
    assert themes[
        ("post_category", POST_THEME_LABELS["other"])
    ] == 0
    assert themes[("signal_type", "expansion")] >= 1


def test_marketing_pack_hides_agencies_and_stale_jobs(tmp_path) -> None:
    _engine, sessions = _database(tmp_path)
    now = datetime(2026, 10, 14, tzinfo=UTC)
    scan = ScanStore.start(sessions, mode="jobs", args={})
    assert scan.save_job(
        _job_payload(
            title="Solar Engineer",
            company="Sun Co",
            industry="green_energy",
            country="United Arab Emirates",
            city="Abu Dhabi",
            region="Abu Dhabi",
            url="https://linkedin.com/jobs/view/9901",
            observed_at=now - timedelta(days=5),
        )
    )
    assert scan.save_job(
        _job_payload(
            title="Agency Recruiter",
            company="Hire Fast",
            industry="healthcare",
            country="United Arab Emirates",
            city="Dubai",
            region="Dubai",
            url="https://linkedin.com/jobs/view/9902",
            observed_at=now - timedelta(days=1),
            posted_by_agency=True,
        )
    )
    assert scan.save_job(
        _job_payload(
            title="Stale Nurse",
            company="Old Clinic",
            industry="healthcare",
            country="United Arab Emirates",
            city="Dubai",
            region="Dubai",
            url="https://linkedin.com/jobs/view/9903",
            observed_at=now - timedelta(days=31),
        )
    )
    scan.finish("completed")
    with sessions.begin() as session:
        jobs = {job.title: job for job in session.scalars(select(Job))}
        sun = Company(
            canonical_name="Sun Co",
            industry_key="renewable_energy",
            is_agency=False,
            jurisdiction="unknown",
        )
        agency = Company(
            canonical_name="Hire Fast",
            industry_key="healthcare",
            is_agency=True,
            jurisdiction="unknown",
        )
        old = Company(
            canonical_name="Old Clinic",
            industry_key="healthcare",
            is_agency=False,
            jurisdiction="unknown",
        )
        session.add_all([sun, agency, old])
        session.flush()
        jobs["Solar Engineer"].company_id = sun.id
        jobs["Agency Recruiter"].company_id = agency.id
        jobs["Stale Nurse"].company_id = old.id

    facts = rolling_hiring(sessions, days=30, hide_agencies=True, now=now)
    tables = marketing_tables(sessions, hide_agencies=True, now=now)
    green = next(
        row
        for row in tables["marketing_industry_30d"]
        if row["industry"] == "green_energy"
    )
    chart = {
        item["industry"]: item
        for item in rolling_industry_chart(facts)["rankings"]
    }
    assert green["job_count"] == chart["green_energy"]["job_count"] == 1
    assert "healthcare" not in chart
    assert all(row["industry"] != "healthcare" for row in tables["marketing_industry_30d"])
    assert [row["company"] for row in tables["marketing_employer_highlights"]] == [
        "Sun Co"
    ]


def test_marketing_zip_matches_csv_pack(tmp_path) -> None:
    _engine, sessions = _database(tmp_path)
    _seed_pipeline(sessions)
    export_marketing_pack(sessions, tmp_path / "marketing")
    archive = zipfile.ZipFile(io.BytesIO(marketing_pack_zip(sessions)))
    assert set(archive.namelist()) == {
        f"{name}.csv" for name in MARKETING_FILES
    } | {REPORT_NAME}
    pulse = (tmp_path / "marketing" / "marketing_weekly_pulse.csv").read_text(
        encoding="utf-8-sig"
    )
    zipped = archive.read("marketing_weekly_pulse.csv").decode("utf-8")
    assert zipped.replace("\r\n", "\n") == pulse.replace("\r\n", "\n")
    assert archive.read(REPORT_NAME).startswith(b"%PDF")


def test_marketing_pdf_has_charts_and_no_pii(tmp_path) -> None:
    _engine, sessions = _database(tmp_path)
    _seed_pipeline(sessions)
    pdf = marketing_report_pdf(sessions)
    assert pdf.startswith(b"%PDF")
    text = _pdf_text(pdf)
    assert "hiring trends briefing" in text
    assert "strongest industries" in text
    assert "healthcare" in text
    assert "dubai" in text
    assert "market insight" in text
    assert "15 days" in text
    assert "aisha" not in text
    assert "rahman" not in text
    assert "@" not in text
    assert "phone" not in text
    assert "email" not in text


def test_market_insight_uses_hiring_industry_and_seniority_only() -> None:
    weights = load_yaml("config/scoring.yaml")["weights"]
    kept = {
        name: {"score_0_100": 100}
        for name in weights
        if name not in MARKET_INSIGHT_EXCLUDED_FACTORS
    }
    ignored = {name: {"score_0_100": 0} for name in MARKET_INSIGHT_EXCLUDED_FACTORS}
    assert set(kept) == {
        "hiring_activity",
        "industry_opportunity",
        "seniority_match",
    }
    assert market_insight({**kept, **ignored}) == 100
    assert market_insight(
        {
            **kept,
            **{name: {"score_0_100": 100} for name in MARKET_INSIGHT_EXCLUDED_FACTORS},
        }
    ) == 100
    assert market_insight({name: {"score_0_100": 0} for name in kept} | ignored) == 0


def test_marketing_insight_averages_match_scored_companies(tmp_path) -> None:
    _engine, sessions = _database(tmp_path)
    _seed_pipeline(sessions)
    scored = pipeline_rows(sessions, PipelineFilters(hide_agencies=True))
    tables = marketing_tables(sessions, hide_agencies=True)
    pulse = tables["marketing_weekly_pulse"][0]
    for name in (
        "avg_market_insight",
        "avg_hiring_activity",
        "avg_industry_opportunity",
        "avg_seniority_match",
    ):
        assert name in pulse
        assert name in tables["marketing_industry_30d"][0]
        assert name in tables["marketing_location_30d"][0]

    healthcare = [row for row in scored if row["industry"] == "healthcare"]
    expected = round(
        sum(float(row["market_insight"]) for row in healthcare) / len(healthcare),
        1,
    )
    industry = next(
        row
        for row in tables["marketing_industry_30d"]
        if row["industry"] == "healthcare"
    )
    assert industry["avg_market_insight"] == expected
    assert pulse["avg_market_insight"] == expected
    dubai = next(
        row for row in tables["marketing_location_30d"] if row["city"] == "Dubai"
    )
    assert dubai["avg_market_insight"] == expected
