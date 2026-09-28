"""SQLAlchemy 2.0 database models."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Declarative model base."""


class ScanRun(Base):
    __tablename__ = "scan_run"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    mode: Mapped[str] = mapped_column(String(32))
    args_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), index=True)
    stats_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class CompanyGroup(Base):
    __tablename__ = "company_group"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True)


class Company(Base):
    __tablename__ = "company"

    id: Mapped[int] = mapped_column(primary_key=True)
    canonical_name: Mapped[str] = mapped_column(String(255), index=True)
    canonical_name_ar: Mapped[str | None] = mapped_column(String(255))
    linkedin_url: Mapped[str | None] = mapped_column(String(1000), unique=True)
    website: Mapped[str | None] = mapped_column(String(1000))
    industry_key: Mapped[str | None] = mapped_column(String(100))
    hq_country: Mapped[str | None] = mapped_column(String(100))
    size_band: Mapped[str | None] = mapped_column(String(50))
    jurisdiction: Mapped[str] = mapped_column(String(32), default="unknown")
    is_agency: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    outreach_status: Mapped[str] = mapped_column(
        String(32), default="new", index=True
    )
    group_id: Mapped[int | None] = mapped_column(ForeignKey("company_group.id"))


class CompanyAlias(Base):
    __tablename__ = "company_alias"
    __table_args__ = (UniqueConstraint("alias_normalised", "source"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("company.id"), index=True)
    alias: Mapped[str] = mapped_column(String(255))
    alias_normalised: Mapped[str] = mapped_column(String(255), index=True)
    source: Mapped[str] = mapped_column(String(100))


class CompanyMergeReview(Base):
    __tablename__ = "company_merge_review"
    __table_args__ = (
        UniqueConstraint("source_company_id", "candidate_company_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_company_id: Mapped[int] = mapped_column(
        ForeignKey("company.id"), index=True
    )
    candidate_company_id: Mapped[int] = mapped_column(
        ForeignKey("company.id"), index=True
    )
    similarity: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SourceRecord(Base):
    __tablename__ = "source_record"
    __table_args__ = (UniqueConstraint("scan_run_id", "source", "source_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    scan_run_id: Mapped[int] = mapped_column(ForeignKey("scan_run.id"), index=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    source_key: Mapped[str] = mapped_column(String(128), index=True)
    source_url: Mapped[str | None] = mapped_column(String(2000))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    raw_text: Mapped[str] = mapped_column(Text)
    raw_fields_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    processing_status: Mapped[str] = mapped_column(String(16), default="new", index=True)
    extraction_attempts: Mapped[int] = mapped_column(Integer, default=0)
    processing_error: Mapped[str | None] = mapped_column(Text)


class Job(Base):
    __tablename__ = "job"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_key: Mapped[str] = mapped_column(String(128), unique=True)
    linkedin_job_id: Mapped[str | None] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(String(500))
    company_name_raw: Mapped[str] = mapped_column(String(500))
    company_linkedin_url: Mapped[str | None] = mapped_column(String(2000))
    company_id: Mapped[int | None] = mapped_column(ForeignKey("company.id"), index=True)
    location_raw: Mapped[str] = mapped_column(String(500))
    city: Mapped[str | None] = mapped_column(String(100))
    region: Mapped[str | None] = mapped_column(String(100))
    country: Mapped[str | None] = mapped_column(String(100))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    observations: Mapped[list[JobObservation]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )


class JobObservation(Base):
    __tablename__ = "job_observation"
    __table_args__ = (UniqueConstraint("job_id", "scan_run_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("job.id"), index=True)
    scan_run_id: Mapped[int] = mapped_column(ForeignKey("scan_run.id"), index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    posted_date: Mapped[date | None] = mapped_column(Date)
    posted_date_precision: Mapped[str] = mapped_column(String(16), default="unknown")
    is_reposted: Mapped[bool] = mapped_column(Boolean, default=False)
    applicant_count: Mapped[int | None] = mapped_column(Integer)
    is_promoted: Mapped[bool] = mapped_column(Boolean, default=False)
    raw_listing_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    job: Mapped[Job] = relationship(back_populates="observations")


class Post(Base):
    __tablename__ = "post"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_key: Mapped[str] = mapped_column(String(128), unique=True)
    post_url: Mapped[str | None] = mapped_column(String(2000))
    author_name: Mapped[str | None] = mapped_column(String(500))
    author_url: Mapped[str | None] = mapped_column(String(2000))
    author_headline: Mapped[str | None] = mapped_column(String(1000))
    author_type: Mapped[str] = mapped_column(String(16), default="unknown")
    author_is_agency: Mapped[bool] = mapped_column(Boolean, default=False)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("company.id"), index=True)
    text: Mapped[str] = mapped_column(Text)
    post_date: Mapped[date | None] = mapped_column(Date)
    post_date_precision: Mapped[str] = mapped_column(String(16), default="unknown")
    post_category: Mapped[str] = mapped_column(String(32), default="other")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class QueryResultCount(Base):
    __tablename__ = "query_result_count"
    __table_args__ = (
        UniqueConstraint(
            "scan_run_id", "source", "keywords", "location", "time_filter"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    scan_run_id: Mapped[int] = mapped_column(ForeignKey("scan_run.id"), index=True)
    source: Mapped[str] = mapped_column(String(32))
    keywords: Mapped[str] = mapped_column(Text)
    location: Mapped[str] = mapped_column(String(255))
    time_filter: Mapped[str] = mapped_column(String(32))
    total_results: Mapped[int | None] = mapped_column(Integer)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Extraction(Base):
    __tablename__ = "extraction"
    __table_args__ = (
        UniqueConstraint(
            "source_record_id", "prompt_version", "model_name"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_record_id: Mapped[int] = mapped_column(
        ForeignKey("source_record.id"), index=True
    )
    cache_key: Mapped[str] = mapped_column(String(64), index=True)
    prompt_version: Mapped[str] = mapped_column(String(100))
    model_name: Mapped[str] = mapped_column(String(255))
    result_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Signal(Base):
    __tablename__ = "signal"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("company.id"), index=True)
    scan_run_id: Mapped[int] = mapped_column(ForeignKey("scan_run.id"), index=True)
    signal_type: Mapped[str] = mapped_column(String(64), index=True)
    strength: Mapped[float] = mapped_column(Float)
    window: Mapped[str] = mapped_column(String(100))
    basis: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Evidence(Base):
    __tablename__ = "evidence"

    id: Mapped[int] = mapped_column(primary_key=True)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signal.id"), index=True)
    source_record_id: Mapped[int] = mapped_column(
        ForeignKey("source_record.id"), index=True
    )
    quoted_text: Mapped[str] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(String(2000))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    basis: Mapped[str] = mapped_column(String(16))


class DecisionMaker(Base):
    __tablename__ = "decision_maker"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("company.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    designation: Mapped[str] = mapped_column(String(255))
    department: Mapped[str | None] = mapped_column(String(255))
    relevance_rank: Mapped[int] = mapped_column(Integer)
    profile_url: Mapped[str | None] = mapped_column(String(2000))
    source: Mapped[str] = mapped_column(String(255))
    verified_by: Mapped[str | None] = mapped_column(String(255))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confidence: Mapped[float | None] = mapped_column(Float)
    notes: Mapped[str | None] = mapped_column(Text)


class LeadScore(Base):
    __tablename__ = "lead_score"
    __table_args__ = (UniqueConstraint("company_id", "scan_run_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("company.id"), index=True)
    scan_run_id: Mapped[int] = mapped_column(ForeignKey("scan_run.id"), index=True)
    total_score: Mapped[int] = mapped_column(Integer)
    priority: Mapped[str] = mapped_column(String(16), index=True)
    factor_results_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    explanation_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    explanation_text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

