"""Database engine and session construction."""

from __future__ import annotations

import hashlib

from sqlalchemy import Engine, create_engine, inspect, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.orm.attributes import flag_modified

from aambridge.db.models import Base, JobObservation, Post, SourceRecord
from aambridge.privacy import privacy_safe, redact_personal_contacts


def create_database_engine(database_url: str, *, echo: bool = False) -> Engine:
    """Create an engine for SQLite or a PostgreSQL-compatible URL."""

    url = make_url(database_url)
    if url.drivername in {"postgresql", "postgres"}:
        url = url.set(drivername="postgresql+psycopg")
    if url.get_backend_name() == "postgresql":
        query = dict(url.query)
        if "sslmode" not in query:
            query["sslmode"] = "require"
            url = url.set(query=query)
        return create_engine(
            url,
            echo=echo,
            future=True,
            pool_pre_ping=True,
            pool_recycle=300,
            pool_size=3,
            max_overflow=2,
        )
    connect_args = {"check_same_thread": False} if url.get_backend_name() == "sqlite" else {}
    return create_engine(
        url,
        echo=echo,
        future=True,
        pool_pre_ping=True,
        connect_args=connect_args,
    )


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Create sessions that retain loaded values after commits."""

    return sessionmaker(bind=engine, expire_on_commit=False)


def initialise_database(
    engine: Engine,
    *,
    scrub_personal_contacts: bool = True,
) -> None:
    """Create all missing pilot tables without deleting data."""

    Base.metadata.create_all(engine)
    _apply_pilot_additive_migrations(engine)
    if scrub_personal_contacts:
        _scrub_existing_personal_contacts(engine)


def _apply_pilot_additive_migrations(engine: Engine) -> None:
    """Apply safe additive columns while the pilot still uses create_all."""

    columns = {column["name"] for column in inspect(engine).get_columns("company")}
    if "is_agency" not in columns:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "ALTER TABLE company "
                "ADD COLUMN is_agency BOOLEAN NOT NULL DEFAULT FALSE"
            )
            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_company_is_agency "
                "ON company (is_agency)"
            )
    if "outreach_status" not in columns:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "ALTER TABLE company "
                "ADD COLUMN outreach_status VARCHAR(32) NOT NULL DEFAULT 'new'"
            )
            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_company_outreach_status "
                "ON company (outreach_status)"
            )


def _scrub_existing_personal_contacts(engine: Engine) -> None:
    """Remove contact details collected by versions predating Phase 4."""

    with Session(engine) as session, session.begin():
        for source in session.scalars(select(SourceRecord)):
            safe_text = redact_personal_contacts(source.raw_text)
            safe_fields = privacy_safe(source.raw_fields_json)
            if safe_text != source.raw_text:
                source.raw_text = safe_text
                normalised = " ".join(safe_text.casefold().split())
                source.content_hash = hashlib.sha256(
                    normalised.encode("utf-8")
                ).hexdigest()
            if safe_fields != source.raw_fields_json:
                source.raw_fields_json = safe_fields
                flag_modified(source, "raw_fields_json")
        for post in session.scalars(select(Post)):
            post.text = redact_personal_contacts(post.text)
        for observation in session.scalars(select(JobObservation)):
            safe_listing = privacy_safe(observation.raw_listing_json)
            if safe_listing != observation.raw_listing_json:
                observation.raw_listing_json = safe_listing
                flag_modified(observation, "raw_listing_json")


def reset_database(engine: Engine) -> None:
    """Drop and recreate all pilot tables."""

    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

