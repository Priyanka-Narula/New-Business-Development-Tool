"""Marketing-safe aggregated reports. No people or contact columns."""

from __future__ import annotations

import csv
import io
import zipfile
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from aambridge.dashboard.queries import (
    PULSE_DAYS,
    ROLLING_DAYS,
    PipelineFilters,
    pipeline_rows,
    rolling_hiring,
    rolling_industry_chart,
)
from aambridge.db.models import Company, Post, Signal
from aambridge.signals.detect import what_changed

POST_THEMES = (
    "hiring",
    "expansion",
    "project_award",
    "leadership_change",
    "other",
)
POST_THEME_LABELS = {
    "hiring": "hiring",
    "expansion": "expansion",
    "project_award": "project_award",
    "leadership_change": "leadership_change",
    "other": "Unclassified (not hiring, expansion, award, or leadership)",
}
SIGNAL_THEMES = (
    "hiring_acceleration",
    "expansion",
    "localisation_pressure",
    "new_senior_vacancy",
    "new_location",
)
ROLE_LIMIT = 50
HIGHLIGHT_LIMIT = 50
PULSE_COLUMNS = (
    "jobs_15d",
    "employers_15d",
    "jobs_30d",
    "employers_30d",
    "leading_industry",
    "leading_city",
    "new_employers",
    "posts_hiring",
    "posts_expansion",
    "posts_project_award",
    "posts_leadership_change",
    "signals_hiring_acceleration",
    "signals_expansion",
    "signals_localisation_pressure",
    "signals_new_senior_vacancy",
    "signals_new_location",
    "avg_market_insight",
    "avg_hiring_activity",
    "avg_industry_opportunity",
    "avg_seniority_match",
)
INSIGHT_COLUMNS = (
    "avg_market_insight",
    "avg_hiring_activity",
    "avg_industry_opportunity",
    "avg_seniority_match",
)
INDUSTRY_COLUMNS = ("industry", "job_count", "employer_count", *INSIGHT_COLUMNS)
LOCATION_COLUMNS = (
    "country",
    "region",
    "city",
    "job_count",
    "employer_count",
    *INSIGHT_COLUMNS,
)
ROLE_COLUMNS = ("job_title", "count", "top_industry", "top_country")
SENIORITY_COLUMNS = ("seniority", "job_count", "share_pct")
THEME_COLUMNS = ("kind", "name", "count")
HIGHLIGHT_COLUMNS = (
    "company",
    "industry",
    "country_city",
    "active_jobs",
    "roles_sample",
)
MARKETING_FILES = {
    "marketing_weekly_pulse": PULSE_COLUMNS,
    "marketing_industry_30d": INDUSTRY_COLUMNS,
    "marketing_location_30d": LOCATION_COLUMNS,
    "marketing_roles_30d": ROLE_COLUMNS,
    "marketing_seniority_30d": SENIORITY_COLUMNS,
    "marketing_themes_30d": THEME_COLUMNS,
    "marketing_employer_highlights": HIGHLIGHT_COLUMNS,
}
FORBIDDEN_COLUMNS = frozenset(
    {
        "key_contact",
        "designation",
        "company_phone",
        "company_email",
        "email",
        "phone",
        "author_name",
        "author_url",
        "author_headline",
        "post_text",
        "profile_url",
        "lead_score",
        "aambridge_fit",
        "status",
        "outreach_status",
    }
)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _unique_jobs(facts: list[dict[str, Any]]) -> int:
    return len({int(item["job_id"]) for item in facts})


def _unique_employers(facts: list[dict[str, Any]]) -> int:
    return len({int(item["company_id"]) for item in facts if item.get("company_id")})


def _leading_city(facts: list[dict[str, Any]]) -> str:
    counts: Counter[str] = Counter()
    for item in facts:
        city = str(item.get("city") or "").strip()
        if city:
            counts[city] += 1
    return counts.most_common(1)[0][0] if counts else ""


def _post_theme_counts(
    session_factory: sessionmaker[Session],
    *,
    days: int,
    hide_agencies: bool,
    now: datetime,
) -> dict[str, int]:
    cutoff = now - timedelta(days=days)
    with session_factory() as session:
        posts = list(session.scalars(select(Post)))
        agencies = {
            company.id
            for company in session.scalars(select(Company).where(Company.is_agency.is_(True)))
        }
    counts = {name: 0 for name in POST_THEMES}
    for post in posts:
        seen = post.last_seen_at or post.first_seen_at
        if seen is None or _aware(seen) < cutoff:
            continue
        if hide_agencies and (
            post.author_is_agency
            or (post.company_id is not None and post.company_id in agencies)
        ):
            continue
        category = str(post.post_category or "")
        if category in counts:
            counts[category] += 1
    return counts


def _signal_employer_counts(
    session_factory: sessionmaker[Session],
    *,
    days: int,
    hide_agencies: bool,
    now: datetime,
) -> dict[str, int]:
    cutoff = now - timedelta(days=days)
    with session_factory() as session:
        signals = list(session.scalars(select(Signal)))
        agencies = {
            company.id
            for company in session.scalars(select(Company).where(Company.is_agency.is_(True)))
        }
    grouped: dict[str, set[int]] = {name: set() for name in SIGNAL_THEMES}
    for signal in signals:
        created = signal.created_at
        if created is not None and _aware(created) < cutoff:
            continue
        if hide_agencies and signal.company_id in agencies:
            continue
        if signal.signal_type in grouped:
            grouped[signal.signal_type].add(int(signal.company_id))
    return {name: len(company_ids) for name, company_ids in grouped.items()}


def _empty_insight() -> dict[str, float]:
    return {name: 0.0 for name in INSIGHT_COLUMNS}


def _average(values: list[float]) -> float:
    if not values:
        return 0.0
    return round(sum(values) / len(values), 1)


def _insight_from_rows(rows: list[dict[str, Any]]) -> dict[str, float]:
    if not rows:
        return _empty_insight()
    return {
        "avg_market_insight": _average(
            [float(row.get("market_insight") or 0) for row in rows]
        ),
        "avg_hiring_activity": _average(
            [float(row.get("hiring_activity_score") or 0) for row in rows]
        ),
        "avg_industry_opportunity": _average(
            [float(row.get("industry_opportunity_score") or 0) for row in rows]
        ),
        "avg_seniority_match": _average(
            [float(row.get("seniority_match_score") or 0) for row in rows]
        ),
    }


def _insight_by_key(
    rows: list[dict[str, Any]],
    key: str,
) -> dict[str, dict[str, float]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        name = str(row.get(key) or "")
        if name:
            grouped[name].append(row)
    return {name: _insight_from_rows(items) for name, items in grouped.items()}


def _insight_by_location(
    rows: list[dict[str, Any]],
) -> dict[tuple[str, str, str], dict[str, float]]:
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[
            (
                str(row.get("country") or ""),
                str(row.get("region") or ""),
                str(row.get("city") or ""),
            )
        ].append(row)
    return {key: _insight_from_rows(items) for key, items in grouped.items()}


def _pulse_row(
    facts_15d: list[dict[str, Any]],
    facts_30d: list[dict[str, Any]],
    industry_rows: list[dict[str, Any]],
    post_counts: dict[str, int],
    signal_counts: dict[str, int],
    new_employers: int,
    insight: dict[str, float],
) -> dict[str, Any]:
    leader = next(
        (item for item in industry_rows if int(item["job_count"]) > 0),
        None,
    )
    return {
        "jobs_15d": _unique_jobs(facts_15d),
        "employers_15d": _unique_employers(facts_15d),
        "jobs_30d": _unique_jobs(facts_30d),
        "employers_30d": _unique_employers(facts_30d),
        "leading_industry": leader["industry"] if leader else "",
        "leading_city": _leading_city(facts_30d),
        "new_employers": new_employers,
        "posts_hiring": post_counts["hiring"],
        "posts_expansion": post_counts["expansion"],
        "posts_project_award": post_counts["project_award"],
        "posts_leadership_change": post_counts["leadership_change"],
        "signals_hiring_acceleration": signal_counts["hiring_acceleration"],
        "signals_expansion": signal_counts["expansion"],
        "signals_localisation_pressure": signal_counts["localisation_pressure"],
        "signals_new_senior_vacancy": signal_counts["new_senior_vacancy"],
        "signals_new_location": signal_counts["new_location"],
        **insight,
    }


def _industry_rows(
    facts: list[dict[str, Any]],
    insight_by_industry: dict[str, dict[str, float]],
) -> list[dict[str, Any]]:
    return [
        {
            "industry": item["industry"],
            "job_count": int(item["job_count"]),
            "employer_count": int(item["company_count"]),
            **insight_by_industry.get(str(item["industry"]), _empty_insight()),
        }
        for item in rolling_industry_chart(facts)["rankings"]
    ]


def _location_rows(
    facts: list[dict[str, Any]],
    insight_by_location: dict[tuple[str, str, str], dict[str, float]],
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], tuple[set[int], set[int]]] = defaultdict(
        lambda: (set(), set())
    )
    for fact in facts:
        country = str(fact.get("country") or "")
        region = str(fact.get("region") or "")
        city = str(fact.get("city") or "")
        if not country and not city:
            continue
        jobs, companies = grouped[(country, region, city)]
        jobs.add(int(fact["job_id"]))
        if fact.get("company_id"):
            companies.add(int(fact["company_id"]))
    rows = [
        {
            "country": country,
            "region": region,
            "city": city,
            "job_count": len(jobs),
            "employer_count": len(companies),
            **insight_by_location.get((country, region, city), _empty_insight()),
        }
        for (country, region, city), (jobs, companies) in grouped.items()
    ]
    rows.sort(
        key=lambda item: (
            -int(item["job_count"]),
            item["country"],
            item["region"],
            item["city"],
        )
    )
    return rows


def _role_rows(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    titles: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for fact in facts:
        title = str(fact.get("title") or "").strip()
        if not title:
            continue
        titles[title].append(fact)
    ranked = sorted(titles.items(), key=lambda item: (-len(item[1]), item[0]))
    rows: list[dict[str, Any]] = []
    for title, matches in ranked[:ROLE_LIMIT]:
        industries = Counter(
            str(item.get("industry") or "") for item in matches if item.get("industry")
        )
        countries = Counter(
            str(item.get("country") or "") for item in matches if item.get("country")
        )
        rows.append(
            {
                "job_title": title,
                "count": len(matches),
                "top_industry": industries.most_common(1)[0][0] if industries else "",
                "top_country": countries.most_common(1)[0][0] if countries else "",
            }
        )
    return rows


def _seniority_rows(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    for fact in facts:
        counts[str(fact.get("seniority") or "unknown") or "unknown"] += 1
    total = sum(counts.values())
    rows = [
        {
            "seniority": name,
            "job_count": count,
            "share_pct": round((count / total) * 100, 1) if total else 0.0,
        }
        for name, count in counts.items()
    ]
    rows.sort(key=lambda item: (-int(item["job_count"]), item["seniority"]))
    return rows


def _theme_rows(
    post_counts: dict[str, int],
    signal_counts: dict[str, int],
) -> list[dict[str, Any]]:
    rows = [
        {
            "kind": "post_category",
            "name": POST_THEME_LABELS.get(name, name),
            "count": post_counts[name],
        }
        for name in POST_THEMES
    ]
    rows.extend(
        {"kind": "signal_type", "name": name, "count": signal_counts[name]}
        for name in SIGNAL_THEMES
    )
    return rows


def _highlight_rows(
    session_factory: sessionmaker[Session],
    facts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for fact in facts:
        company_id = fact.get("company_id")
        if not company_id:
            continue
        grouped[int(company_id)].append(fact)
    if not grouped:
        return []
    with session_factory() as session:
        companies = {
            company.id: company
            for company in session.scalars(
                select(Company).where(Company.id.in_(list(grouped)))
            )
        }
    rows: list[dict[str, Any]] = []
    for company_id, matches in grouped.items():
        company = companies.get(company_id)
        if company is None or company.is_agency:
            continue
        titles = []
        for fact in matches:
            title = str(fact.get("title") or "").strip()
            if title and title not in titles:
                titles.append(title)
        city = str(matches[0].get("city") or "")
        country = company.hq_country or str(matches[0].get("country") or "")
        rows.append(
            {
                "company": company.canonical_name,
                "industry": company.industry_key
                or str(matches[0].get("industry") or ""),
                "country_city": ", ".join(part for part in (city, country) if part),
                "active_jobs": len({int(item["job_id"]) for item in matches}),
                "roles_sample": "; ".join(titles[:3]),
            }
        )
    rows.sort(key=lambda item: (-int(item["active_jobs"]), item["company"]))
    return rows[:HIGHLIGHT_LIMIT]


def marketing_tables(
    session_factory: sessionmaker[Session],
    *,
    hide_agencies: bool = True,
    now: datetime | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Build every marketing table. Values are counts and public employer names."""

    moment = now or datetime.now(UTC)
    facts_30d = rolling_hiring(
        session_factory,
        days=ROLLING_DAYS,
        hide_agencies=hide_agencies,
        now=moment,
    )
    facts_15d = rolling_hiring(
        session_factory,
        days=PULSE_DAYS,
        hide_agencies=hide_agencies,
        now=moment,
    )
    scored = pipeline_rows(
        session_factory,
        PipelineFilters(hide_agencies=hide_agencies),
    )
    insight = _insight_from_rows(scored)
    industry_rows = _industry_rows(facts_30d, _insight_by_key(scored, "industry"))
    post_counts = _post_theme_counts(
        session_factory, days=ROLLING_DAYS, hide_agencies=hide_agencies, now=moment
    )
    signal_counts = _signal_employer_counts(
        session_factory, days=ROLLING_DAYS, hide_agencies=hide_agencies, now=moment
    )
    changes = what_changed(session_factory)
    return {
        "marketing_weekly_pulse": [
            _pulse_row(
                facts_15d,
                facts_30d,
                industry_rows,
                post_counts,
                signal_counts,
                len(changes.get("new_companies") or []),
                insight,
            )
        ],
        "marketing_industry_30d": industry_rows,
        "marketing_location_30d": _location_rows(
            facts_30d, _insight_by_location(scored)
        ),
        "marketing_roles_30d": _role_rows(facts_30d),
        "marketing_seniority_30d": _seniority_rows(facts_30d),
        "marketing_themes_30d": _theme_rows(post_counts, signal_counts),
        "marketing_employer_highlights": _highlight_rows(session_factory, facts_30d),
    }


def _csv_text(columns: tuple[str, ...], rows: list[dict[str, Any]]) -> str:
    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=list(columns),
        extrasaction="ignore",
        lineterminator="\n",
    )
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def marketing_report_pdf(
    session_factory: sessionmaker[Session],
    *,
    hide_agencies: bool = True,
    now: datetime | None = None,
) -> bytes:
    """PDF briefing with the same trend charts as the dashboard."""

    from aambridge.dashboard.report import render_marketing_report

    moment = now or datetime.now(UTC)
    tables = marketing_tables(
        session_factory, hide_agencies=hide_agencies, now=moment
    )
    return render_marketing_report(tables, now=moment)


def export_marketing_pack(
    session_factory: sessionmaker[Session],
    output_dir: Path,
    *,
    hide_agencies: bool = True,
    now: datetime | None = None,
) -> list[Path]:
    """Write the marketing CSVs and PDF under output_dir and return the paths."""

    from aambridge.dashboard.report import REPORT_NAME, render_marketing_report

    output_dir.mkdir(parents=True, exist_ok=True)
    moment = now or datetime.now(UTC)
    tables = marketing_tables(
        session_factory, hide_agencies=hide_agencies, now=moment
    )
    written: list[Path] = []
    for name, columns in MARKETING_FILES.items():
        path = output_dir / f"{name}.csv"
        with path.open("w", newline="", encoding="utf-8-sig") as handle:
            handle.write(_csv_text(columns, tables[name]))
        written.append(path)
    pdf_path = output_dir / REPORT_NAME
    pdf_path.write_bytes(render_marketing_report(tables, now=moment))
    written.append(pdf_path)
    return written


def marketing_pack_zip(
    session_factory: sessionmaker[Session],
    *,
    hide_agencies: bool = True,
    now: datetime | None = None,
) -> bytes:
    """Zip the marketing CSVs and PDF for a dashboard download."""

    from aambridge.dashboard.report import REPORT_NAME, render_marketing_report

    moment = now or datetime.now(UTC)
    tables = marketing_tables(
        session_factory, hide_agencies=hide_agencies, now=moment
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, columns in MARKETING_FILES.items():
            archive.writestr(f"{name}.csv", _csv_text(columns, tables[name]))
        archive.writestr(REPORT_NAME, render_marketing_report(tables, now=moment))
    return buffer.getvalue()
