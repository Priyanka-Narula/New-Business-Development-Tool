"""Copy a local SQLite database into another SQLAlchemy URL."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import func, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from aambridge.db.models import Base
from aambridge.db.session import (
    create_database_engine,
    initialise_database,
    reset_database,
)


def destination_has_rows(engine: Engine) -> bool:
    """Return whether any mapped table already contains rows."""

    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    with Session(engine) as session:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing:
                continue
            if session.scalar(select(func.count()).select_from(table)):
                return True
    return False


def migrate_sqlite(
    source_path: Path,
    destination_engine: Engine,
    *,
    replace: bool = False,
) -> dict[str, int]:
    """Copy mapped tables from a SQLite file, preserving primary keys."""

    resolved = source_path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"SQLite source not found: {resolved}")

    if destination_has_rows(destination_engine):
        if not replace:
            raise RuntimeError(
                "Destination already has rows. Pass --replace to drop and reload."
            )
        reset_database(destination_engine)
    else:
        initialise_database(destination_engine)

    source_engine = create_database_engine(f"sqlite:///{resolved.as_posix()}")
    counts: dict[str, int] = {}
    try:
        source_tables = set(inspect(source_engine).get_table_names())
        with source_engine.connect() as source, destination_engine.begin() as dest:
            for table in Base.metadata.sorted_tables:
                if table.name not in source_tables:
                    continue
                rows = [dict(row) for row in source.execute(select(table)).mappings()]
                if rows:
                    dest.execute(table.insert(), rows)
                counts[table.name] = len(rows)
        if destination_engine.dialect.name == "postgresql":
            _reset_postgres_sequences(destination_engine)
    finally:
        source_engine.dispose()
    return counts


def _reset_postgres_sequences(engine: Engine) -> None:
    """Advance identity sequences past the highest copied primary key."""

    with engine.begin() as connection:
        preparer = connection.dialect.identifier_preparer
        for table in Base.metadata.sorted_tables:
            if "id" not in table.c:
                continue
            quoted = preparer.quote(table.name)
            connection.execute(
                text(
                    "SELECT setval("
                    f"pg_get_serial_sequence('{table.name}', 'id'), "
                    f"COALESCE((SELECT MAX(id) FROM {quoted}), 1), "
                    f"(SELECT MAX(id) FROM {quoted}) IS NOT NULL)"
                )
            )
