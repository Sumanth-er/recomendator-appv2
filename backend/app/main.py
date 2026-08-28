"""Application entry point.

Tables are created on startup if they are not already there, so a fresh Cloud
SQL instance needs no migration step for this POC.

Import order in this file is load bearing. Logging and the tracer provider are
installed before anything that instruments itself on import - the database
engine, the routers, and through them ADK - so that every one of those binds to
a live provider rather than to a no-op. That is why the imports below the
setup calls carry a noqa rather than being hoisted to the top.
"""
from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from opentelemetry import trace as otel_trace

from . import telemetry
from .config import settings


class CloudLoggingFormatter(logging.Formatter):
    """One JSON object per line, in the shape Cloud Logging parses.

    Cloud Run collects stdout on its own, so this is the whole logging
    pipeline in normal operation - no API calls, no extra IAM. The trace and
    span ids are what make a log line clickable from its span in Trace
    Explorer, and vice versa.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "severity": record.levelname,
            "message": super().format(record),
            "logging.googleapis.com/sourceLocation": {
                "file": record.pathname,
                "line": record.lineno,
                "function": record.funcName,
            },
        }
        try:
            ctx = otel_trace.get_current_span().get_span_context()
            if ctx.is_valid and settings.project_id:
                payload["logging.googleapis.com/trace"] = (
                    f"projects/{settings.project_id}/traces/{ctx.trace_id:032x}")
                payload["logging.googleapis.com/spanId"] = f"{ctx.span_id:016x}"
                payload["logging.googleapis.com/trace_sampled"] = (
                    ctx.trace_flags.sampled)
        except Exception:  # noqa: BLE001 - a log line must never raise
            pass
        return json.dumps(payload, default=str)


def _configure_logging() -> None:
    """Install the formatter as the root handler.

    basicConfig has to be given the handler explicitly. Called without one it
    builds a plain StreamHandler of its own, which is how a configured
    formatter ends up never being used.
    """
    handler = logging.StreamHandler()
    handler.setFormatter(CloudLoggingFormatter("%(message)s"))
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        handlers=[handler],
        force=True,
    )


_configure_logging()
telemetry.setup()
telemetry.instrument_requests()

from .api.routes import router  # noqa: E402 - must follow telemetry.setup()
from .db import init_db  # noqa: E402

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Schema creation and seeding run about 130 statements, and start-up is
    # outside any request, so without a span of their own each one becomes its
    # own root trace - 130 of them on every cold start, which on a service that
    # scales to zero buries the traces anyone actually wants to look at. One
    # parent span turns that into a single, collapsible startup trace.
    with telemetry.tracer().start_as_current_span("app.startup") as span:
        init_db()
        telemetry.set_attributes(span, **{"startup.phase": "init_db"})

    log.info(
        "started: bucket=%s processor=%s model=%s vertex_location=%s telemetry=%s",
        settings.bucket or "-", settings.docai_processor_id or "-",
        settings.vertex_model, settings.vertex_location, telemetry.status(),
    )
    try:
        yield
    finally:
        # Cloud Run stops the container shortly after this returns; anything
        # still sitting in a batch processor is lost unless it goes now.
        telemetry.flush()
        telemetry.shutdown()


app = FastAPI(title="Sourcing agent POC", version=settings.engine_version,
              lifespan=lifespan)

# The frontend is a separate Cloud Run service, so it is a cross origin caller.
# Open CORS matches the no-auth scope of this POC.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)

# Health checks would otherwise be most of the trace volume.
telemetry.instrument_fastapi(app, excluded_urls="/healthz")


@app.get("/healthz")
def healthz():
    return {
        "status": "ok",
        "engine_version": settings.engine_version,
        "telemetry": telemetry.status(),
    }


# Local convenience: serve the SPA from the same process so the whole POC runs
# with one command. In Cloud Run the frontend is its own container and this
# stays switched off.
if os.getenv("SERVE_FRONTEND", "0") == "1":
    frontend = Path(__file__).resolve().parents[2] / "frontend"
    if frontend.is_dir():
        app.mount("/", StaticFiles(directory=frontend, html=True), name="frontend")
        log.info("serving frontend from %s", frontend)
