"""Database engine, session and startup table creation."""
from __future__ import annotations

import logging
from collections.abc import Iterator

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session, sessionmaker

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
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db() -> None:
    """Create any table that does not yet exist, then seed reference data.

    create_all is a no-op for tables that are already present, so this is safe
    to run on every Cloud Run cold start.
    """
    Base.metadata.create_all(bind=engine)
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
