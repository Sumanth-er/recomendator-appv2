"""Database engine, session and startup table creation."""
from __future__ import annotations

import logging
from collections.abc import Iterator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker


from . import telemetry
from .config import settings
from .models import Base

log = logging.getLogger(__name__)

url = settings.sqlalchemy_url()
is_sqlite = str(url).startswith("sqlite")
engine = create_engine(
    url,
    pool_pre_ping=True,
    pool_recycle=1800,
    connect_args={"check_same_thread": False} if is_sqlite else {},
)
# Emits a span per statement, parented to whatever span is current - the HTTP
# request, an ingest stage, or an agent tool call. Optional: a missing
# instrumentation package costs the query spans, not the database.
telemetry.instrument_sqlalchemy(engine)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def _ensure_column(table: str, column: str, ddl_type: str, default_sql: str) -> None:
    """Add a column to a table that already exists, if it's missing.

    create_all only creates missing tables, not missing columns on a table
    Cloud Run already created on a prior cold start - so a schema change
    (like ComplianceRequirement.manual_override) needs this instead. Safe to
    call on every startup: a no-op once the column is there.
    """
    existing = {c["name"] for c in inspect(engine).get_columns(table)}
    if column in existing:
        return
    try:
        with engine.begin() as conn:
            conn.execute(text(
                f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type} DEFAULT {default_sql}"
            ))
    except Exception:
        # Two Cloud Run instances starting together both see the column
        # missing; the second ALTER then fails. Losing that race is fine -
        # anything else is not.
        if column in {c["name"] for c in inspect(engine).get_columns(table)}:
            return
        raise
    log.info("added column %s.%s", table, column)


def init_db() -> None:
    """Create any table that does not yet exist, then seed reference data.

    create_all is a no-op for tables that are already present, so this is safe
    to run on every Cloud Run cold start.
    """
    Base.metadata.create_all(bind=engine)
    _ensure_column("compliance_requirement", "manual_override", "BOOLEAN", "FALSE")
    _ensure_column("chat_message", "actions", "JSON", "NULL")
    created = inspect(engine).get_table_names()
    log.info("schema ready, %d tables present", len(created))

    # Reference data is configuration the engine cannot run without - densities,
    # ceilings, required volumes, the compliance checklist, the thresholds. It
    # is not sample data, and no supplier, quote or comparison is ever seeded.
    from .seed import seed_reference_data
    with SessionLocal() as session:
        seed_reference_data(session)


def get_session() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
