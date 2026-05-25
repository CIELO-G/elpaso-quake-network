"""Station list endpoint."""

from __future__ import annotations

from fastapi import APIRouter

from dashboard.deps import load_stations

router = APIRouter()


@router.get("/api/stations")
async def stations():
    return load_stations()
