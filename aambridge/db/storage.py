"""Incremental scan persistence and database-backed CSV export."""

from __future__ import annotations

import csv
import hashlib
import json
import re
from collections.abc import Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from aambridge.db.models import (
    Job,
    JobObservation,
    Post,
    QueryResultCount,
    ScanRun,
    SourceRecord,
)
from aambridge.privacy import privacy_safe

JOB_ID_RE = re.compile(r"/jobs/view/(?:[^/?]+-)?(\d+)")
POST_URN_RE = re.compile(r"urn:li:(?:activity|ugcPost|share):(\d+)")


def utc_now() -> datetime:
    """Return an aware UTC timestamp."""

    return datetime.now(UTC)


def normalise_text(value: str) -> str:
    """Normalise whitespace and case for stable hashing."""

    return " ".join((value or "").casefold().split())


def text_hash(value: str) -> str:
    """Return the SHA-256 hash of normalised text."""

    return hashlib.sha256(normalise_text(value).encode("utf-8")).hexdigest()


def job_source_key(job_url: str, raw_text: str) -> str:
    """Use the LinkedIn job ID, falling back to a text hash."""

    match = JOB_ID_RE.search(job_url or "")
    return match.group(1) if match else text_hash(raw_text)


def post_source_key(post_url: str, raw_text: str) -> str:
    """Use the LinkedIn post URN, falling back to a text hash."""

    match = POST_URN_RE.search(post_url or "")
    return match.group(1) if match else text_hash(raw_text)


def _json_safe(value: Mapping[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(privacy_safe(dict(value)), default=str))


def _datetime_value(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            pass
    return utc_now()


def _date_value(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def empty_stats() -> dict[str, Any]:
    """Create the required scan-run statistics shape."""

    return {
        "pages_loaded": 0,
        "cards_seen": 0,
        "cards_parsed": 0,
        "parse_failures_by_selector": {},
        "rows_saved": 0,
    }


class ScanStore:
    """Persist one scan incrementally, committing every parsed record."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        scan_run_id: int,
    ) -> None:
        self._session_factory = session_factory
        self.scan_run_id = scan_run_id

    @classmethod
    def start(
        cls,
        session_factory: sessionmaker[Session],
        *,
        mode: str,
        args: Mapping[str, Any],
    ) -> ScanStore:
        with session_factory.begin() as session:
            scan = ScanRun(
                started_at=utc_now(),
                finished_at=None,
                mode=mode,
                args_json=_json_safe(args),
                status="running",
                stats_json=empty_stats(),
            )
            session.add(scan)
            session.flush()
            scan_run_id = scan.id
        return cls(session_factory, scan_run_id)

    def _increment_stats(
        self,
        session: Session,
        *,
        pages_loaded: int = 0,
        cards_seen: int = 0,
        cards_parsed: int = 0,
        rows_saved: int = 0,
        failed_selector: str | None = None,
    ) -> None:
        scan = session.get(ScanRun, self.scan_run_id)
        if scan is None:
            raise RuntimeError(f"Scan run {self.scan_run_id} no longer exists")
        stats = dict(scan.stats_json or empty_stats())
        stats["pages_loaded"] = int(stats.get("pages_loaded", 0)) + pages_loaded
        stats["cards_seen"] = int(stats.get("cards_seen", 0)) + cards_seen
        stats["cards_parsed"] = int(stats.get("cards_parsed", 0)) + cards_parsed
        stats["rows_saved"] = int(stats.get("rows_saved", 0)) + rows_saved
        failures = dict(stats.get("parse_failures_by_selector", {}))
        if failed_selector:
            failures[failed_selector] = int(failures.get(failed_selector, 0)) + 1
        stats["parse_failures_by_selector"] = failures
        scan.stats_json = stats

    def page_loaded(self, cards_seen: int) -> None:
        """Record one successfully loaded result page."""

        with self._session_factory.begin() as session:
            self._increment_stats(
                session, pages_loaded=1, cards_seen=max(cards_seen, 0)
            )

    def parse_failure(self, selector: str) -> None:
        """Record a selector-specific parse failure."""

        with self._session_factory.begin() as session:
            self._increment_stats(session, failed_selector=selector)

    def card_parsed(self) -> None:
        """Record a card that was parsed, independently of deduplication."""

        with self._session_factory.begin() as session:
            self._increment_stats(session, cards_parsed=1)

    def save_job(self, values: Mapping[str, Any]) -> bool:
        """Upsert a job and add at most one observation for this scan."""

        from aambridge.contacts.listing import listing_emails, parse_end_client_name

        original_blob = " ".join(
            str(values.get(key) or "")
            for key in ("description", "raw_text", "job_title", "company_name")
        )
        posted_by_agency = bool(values.get("posted_by_agency"))
        copied_emails = [] if posted_by_agency else listing_emails(original_blob)
        end_client_name = (
            parse_end_client_name(original_blob) if posted_by_agency else ""
        )
        hint = values.get("end_client_hint")
        if posted_by_agency and not end_client_name and isinstance(hint, Mapping):
            end_client_name = str(hint.get("company_name") or "")

        fields = _json_safe(values)
        fields["listing_emails"] = copied_emails
        fields["end_client_name"] = end_client_name
        observed_at = _datetime_value(fields.get("observed_at"))
        raw_text = str(fields.get("raw_text") or "").strip() or " ".join(
            str(fields.get(key, ""))
            for key in ("job_title", "company_name", "job_location", "description")
        ).strip()
        source_url = str(fields.get("source_url") or fields.get("job_url") or "")
        source_key = str(fields.get("source_key") or "") or job_source_key(
            source_url, raw_text
        )

        with self._session_factory.begin() as session:
            existing_source = session.scalar(
                select(SourceRecord).where(
                    SourceRecord.scan_run_id == self.scan_run_id,
                    SourceRecord.source == "linkedin_job",
                    SourceRecord.source_key == source_key,
                )
            )
            if existing_source is not None:
                return False

            source_record = SourceRecord(
                scan_run_id=self.scan_run_id,
                source="linkedin_job",
                source_key=source_key,
                source_url=source_url or None,
                observed_at=observed_at,
                raw_text=raw_text,
                raw_fields_json=fields,
                content_hash=text_hash(raw_text),
                processing_status="new",
                extraction_attempts=0,
            )
            session.add(source_record)

            job = session.scalar(select(Job).where(Job.source_key == source_key))
            if job is None:
                job = Job(
                    source_key=source_key,
                    linkedin_job_id=(
                        str(fields.get("linkedin_job_id") or "")
                        or (source_key if source_key.isdigit() else None)
                    ),
                    title=str(fields.get("job_title", "")),
                    company_name_raw=str(fields.get("company_name", "")),
                    company_linkedin_url=fields.get("company_linkedin_url") or None,
                    location_raw=str(fields.get("job_location", "")),
                    city=fields.get("city") or None,
                    region=fields.get("region") or None,
                    country=fields.get("country") or None,
                    first_seen_at=observed_at,
                    last_seen_at=observed_at,
                    is_active=True,
                )
                session.add(job)
                session.flush()
            else:
                job.title = str(fields.get("job_title", job.title))
                job.company_name_raw = str(
                    fields.get("company_name", job.company_name_raw)
                )
                job.company_linkedin_url = (
                    fields.get("company_linkedin_url")
                    or job.company_linkedin_url
                )
                job.location_raw = str(fields.get("job_location", job.location_raw))
                job.city = fields.get("city") or job.city
                job.region = fields.get("region") or job.region
                job.country = fields.get("country") or job.country
                job.last_seen_at = observed_at
                job.is_active = True

            observation = session.scalar(
                select(JobObservation).where(
                    JobObservation.job_id == job.id,
                    JobObservation.scan_run_id == self.scan_run_id,
                )
            )
            if observation is None:
                session.add(
                    JobObservation(
                        job_id=job.id,
                        scan_run_id=self.scan_run_id,
                        observed_at=observed_at,
                        posted_date=_date_value(fields.get("posted_date")),
                        posted_date_precision=str(
                            fields.get("posted_date_precision", "unknown")
                        ),
                        is_reposted=bool(fields.get("is_reposted", False)),
                        applicant_count=fields.get("applicant_count"),
                        is_promoted=bool(fields.get("is_promoted", False)),
                        raw_listing_json=fields,
                    )
                )
            self._increment_stats(session, rows_saved=1)
        return True

    def query_result_count(
        self,
        *,
        source: str,
        keywords: str,
        location: str,
        time_filter: str,
        total_results: int | None,
    ) -> None:
        """Store the page-one result count for market-demand time series."""

        with self._session_factory.begin() as session:
            existing = session.scalar(
                select(QueryResultCount).where(
                    QueryResultCount.scan_run_id == self.scan_run_id,
                    QueryResultCount.source == source,
                    QueryResultCount.keywords == keywords,
                    QueryResultCount.location == location,
                    QueryResultCount.time_filter == time_filter,
                )
            )
            if existing is None:
                session.add(
                    QueryResultCount(
                        scan_run_id=self.scan_run_id,
                        source=source,
                        keywords=keywords,
                        location=location,
                        time_filter=time_filter,
                        total_results=total_results,
                        observed_at=utc_now(),
                    )
                )
            else:
                existing.total_results = total_results
                existing.observed_at = utc_now()

    def save_post(self, values: Mapping[str, Any]) -> bool:
        """Upsert a post while preserving a source record for this scan."""

        fields = _json_safe(values)
        observed_at = _datetime_value(fields.get("observed_at"))
        raw_text = str(fields.get("raw_text") or fields.get("post_text") or "")
        source_url = str(fields.get("source_url") or fields.get("post_url") or "")
        source_key = str(fields.get("source_key") or "") or post_source_key(
            source_url, raw_text
        )

        with self._session_factory.begin() as session:
            existing_source = session.scalar(
                select(SourceRecord).where(
                    SourceRecord.scan_run_id == self.scan_run_id,
                    SourceRecord.source == "linkedin_post",
                    SourceRecord.source_key == source_key,
                )
            )
            if existing_source is not None:
                return False

            session.add(
                SourceRecord(
                    scan_run_id=self.scan_run_id,
                    source="linkedin_post",
                    source_key=source_key,
                    source_url=source_url or None,
                    observed_at=observed_at,
                    raw_text=raw_text,
                    raw_fields_json=fields,
                    content_hash=text_hash(raw_text),
                    processing_status="new",
                    extraction_attempts=0,
                )
            )

            post = session.scalar(select(Post).where(Post.source_key == source_key))
            if post is None:
                session.add(
                    Post(
                        source_key=source_key,
                        post_url=source_url or None,
                        author_name=fields.get("author_name") or None,
                        author_url=fields.get("author_url") or None,
                        author_headline=fields.get("author_headline") or None,
                        author_type=str(fields.get("author_type", "unknown")),
                        author_is_agency=bool(fields.get("author_is_agency", False)),
                        text=raw_text,
                        post_date=_date_value(fields.get("post_date")),
                        post_date_precision=str(
                            fields.get("post_date_precision", "unknown")
                        ),
                        post_category=str(fields.get("post_category", "other")),
                        first_seen_at=observed_at,
                        last_seen_at=observed_at,
                    )
                )
            else:
                post.text = raw_text
                post.post_url = source_url or post.post_url
                post.author_name = fields.get("author_name") or post.author_name
                post.author_url = fields.get("author_url") or post.author_url
                post.author_headline = (
                    fields.get("author_headline") or post.author_headline
                )
                post.author_type = str(
                    fields.get("author_type") or post.author_type
                )
                post.author_is_agency = bool(
                    fields.get("author_is_agency", post.author_is_agency)
                )
                post.post_date = _date_value(fields.get("post_date")) or post.post_date
                post.post_date_precision = str(
                    fields.get("post_date_precision") or post.post_date_precision
                )
                post.post_category = str(
                    fields.get("post_category") or post.post_category
                )
                post.last_seen_at = observed_at
            self._increment_stats(session, rows_saved=1)
        return True

    def finish(self, status: str) -> None:
        """Finish the scan with a terminal status."""

        if status not in {"completed", "interrupted", "failed"}:
            raise ValueError(f"Invalid terminal scan status: {status}")
        with self._session_factory.begin() as session:
            scan = session.get(ScanRun, self.scan_run_id)
            if scan is None:
                raise RuntimeError(f"Scan run {self.scan_run_id} no longer exists")
            scan.status = status
            scan.finished_at = utc_now()

    def stats(self) -> dict[str, Any]:
        """Return a snapshot of persisted scan statistics."""

        with self._session_factory() as session:
            scan = session.get(ScanRun, self.scan_run_id)
            if scan is None:
                raise RuntimeError(f"Scan run {self.scan_run_id} no longer exists")
            return dict(scan.stats_json)


def _write_csv(path: Path, rows: list[dict[str, Any]], columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def export_csv(session_factory: sessionmaker[Session], output_dir: Path) -> None:
    """Export current canonical jobs and posts from the database."""

    with session_factory() as session:
        jobs = list(session.scalars(select(Job).order_by(Job.id)))
        posts = list(session.scalars(select(Post).order_by(Post.id)))
        post_sources = list(
            session.scalars(
                select(SourceRecord)
                .where(SourceRecord.source == "linkedin_post")
                .order_by(SourceRecord.observed_at)
            )
        )
    latest_post_fields = {
        source.source_key: source.raw_fields_json for source in post_sources
    }

    job_columns = [
        "source_key",
        "linkedin_job_id",
        "job_title",
        "company_name",
        "company_linkedin_url",
        "job_location",
        "city",
        "region",
        "country",
        "first_seen_at",
        "last_seen_at",
        "is_active",
        "job_url",
    ]
    job_rows = [
        {
            "source_key": job.source_key,
            "linkedin_job_id": job.linkedin_job_id or "",
            "job_title": job.title,
            "company_name": job.company_name_raw,
            "company_linkedin_url": job.company_linkedin_url or "",
            "job_location": job.location_raw,
            "city": job.city or "",
            "region": job.region or "",
            "country": job.country or "",
            "first_seen_at": job.first_seen_at.isoformat(),
            "last_seen_at": job.last_seen_at.isoformat(),
            "is_active": job.is_active,
            "job_url": (
                f"https://www.linkedin.com/jobs/view/{job.linkedin_job_id}"
                if job.linkedin_job_id
                else ""
            ),
        }
        for job in jobs
    ]
    _write_csv(output_dir / "linkedin_me_jobs.csv", job_rows, job_columns)

    post_columns = [
        "source_key",
        "post_url",
        "author_name",
        "author_url",
        "author_headline",
        "author_type",
        "author_is_agency",
        "has_contact_email",
        "post_text",
        "post_date",
        "post_date_precision",
        "post_category",
        "first_seen_at",
        "last_seen_at",
    ]
    post_rows = [
        {
            "source_key": post.source_key,
            "post_url": post.post_url or "",
            "author_name": post.author_name or "",
            "author_url": post.author_url or "",
            "author_headline": post.author_headline or "",
            "author_type": post.author_type,
            "author_is_agency": post.author_is_agency,
            "has_contact_email": bool(
                latest_post_fields.get(post.source_key, {}).get(
                    "has_contact_email", False
                )
            ),
            "post_text": post.text,
            "post_date": post.post_date.isoformat() if post.post_date else "",
            "post_date_precision": post.post_date_precision,
            "post_category": post.post_category,
            "first_seen_at": post.first_seen_at.isoformat(),
            "last_seen_at": post.last_seen_at.isoformat(),
        }
        for post in posts
    ]
    _write_csv(output_dir / "linkedin_me_posts.csv", post_rows, post_columns)

