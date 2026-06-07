"""FastAPI app construction: middleware + router registration.

Each domain (system, pipeline, stations, catalog, waveforms, review, export)
lives in its own router module under ``dashboard/routes/``. This file just
wires them up. Keep it small — anything more than middleware config or
router includes belongs in a router module or a shared helper.
"""

from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from dashboard.deps import STATIC_DIR
from dashboard.middleware import BasicAuthMiddleware, RateLimitMiddleware
from dashboard.routes import (
    admin as admin_routes,
    catalog as catalog_routes,
    export as export_routes,
    pipeline as pipeline_routes,
    review as review_routes,
    stations as stations_routes,
    system as system_routes,
    waveforms as waveforms_routes,
)

app = FastAPI(title="El Paso Seismic Monitor")

# ── CORS ─────────────────────────────────────────────────────────
_CORS_ORIGINS = os.environ.get(
    "CORS_ORIGINS", "http://localhost:8000,http://127.0.0.1:8000"
).split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Auth + rate limiting ─────────────────────────────────────────
app.add_middleware(BasicAuthMiddleware)
app.add_middleware(RateLimitMiddleware)

# ── Static assets ────────────────────────────────────────────────
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# ── Routers ──────────────────────────────────────────────────────
app.include_router(system_routes.router)
app.include_router(stations_routes.router)
app.include_router(pipeline_routes.router)
app.include_router(catalog_routes.router)
app.include_router(waveforms_routes.router)
app.include_router(review_routes.router)
app.include_router(export_routes.router)
app.include_router(admin_routes.router)
