"""Application entry point.

Tables are created on startup if they are not already there, so a fresh Cloud
SQL instance needs no migration step for this POC.
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .api.routes import router
from .config import settings
from .db import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    log.info(
        "started: bucket=%s processor=%s model=%s vertex_location=%s",
        settings.bucket or "-", settings.docai_processor_id or "-",
        settings.vertex_model, settings.vertex_location,
    )
    yield


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

@app.get("/healthz")
def healthz():
    return {"status": "ok", "engine_version": settings.engine_version}


# Local convenience: serve the SPA from the same process so the whole POC runs
# with one command. In Cloud Run the frontend is its own container and this
# stays switched off.
if os.getenv("SERVE_FRONTEND", "0") == "1":
    frontend = Path(__file__).resolve().parents[2] / "frontend"
    if frontend.is_dir():
        app.mount("/", StaticFiles(directory=frontend, html=True), name="frontend")
        log.info("serving frontend from %s", frontend)
