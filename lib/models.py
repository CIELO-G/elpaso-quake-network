"""Data models for the El Paso seismic pipeline.

Provides Pydantic models for configuration validation and dataclasses
for runtime data structures (Pick, Event, Station).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Runtime data structures
# ---------------------------------------------------------------------------


@dataclass
class Station:
    """A seismic station in the network."""

    network: str
    station: str
    latitude: float
    longitude: float
    elevation_m: float
    location: str = "00"
    channels: str = "EHZ"
    sample_rate_hz: float = 100.0
    model: str = ""
    start_date: str = ""
    fdsnws_url: str | None = None
    instrument: dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        """Network.Station identifier."""
        return f"{self.network}.{self.station}"

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Station:
        """Create a Station from a stations.json entry."""
        return cls(
            network=d["network"],
            station=d["station"],
            latitude=d["latitude"],
            longitude=d["longitude"],
            elevation_m=d["elevation_m"],
            location=d.get("location", "00"),
            channels=d.get("channels", "EHZ"),
            sample_rate_hz=d.get("sample_rate_hz", 100.0),
            model=d.get("model", ""),
            start_date=d.get("start_date", ""),
            fdsnws_url=d.get("fdsnws_url"),
            instrument=d.get("instrument", {}),
        )


@dataclass
class Pick:
    """A seismic phase pick."""

    network: str
    station: str
    location: str
    channel: str
    phase: str  # "P" or "S"
    time: str  # ISO 8601 timestamp
    probability: float
    model: str = ""
    amplitude: float | None = None
    amplitude_channel: str = ""


@dataclass
class Event:
    """A seismic event (earthquake)."""

    event_id: str
    time: str
    latitude: float
    longitude: float
    depth_km: float
    magnitude: float | None = None
    magnitude_type: str = ""
    ml_err: float | None = None
    sigma_time: float = 0.0
    sigma_amp: float = 0.0
    num_picks: int = 0
    num_ml_sta: int = 0


# ---------------------------------------------------------------------------
# Station validation
# ---------------------------------------------------------------------------

REQUIRED_STATION_FIELDS = {"network", "station", "latitude", "longitude", "elevation_m"}
VALID_CHANNEL_PATTERNS = {"EHZ", "EH?", "E??", "HH?", "HHZ", "EN?", "SHZ", "BHZ", "BH?"}


class StationValidationError(ValueError):
    """Raised when station data fails validation."""


def validate_station(entry: dict[str, Any], index: int) -> list[str]:
    """Validate a single station entry from stations.json.

    Returns a list of warning/error messages. Raises StationValidationError
    for critical issues.
    """
    warnings: list[str] = []

    # Required fields
    missing = REQUIRED_STATION_FIELDS - set(entry.keys())
    if missing:
        raise StationValidationError(f"Station entry {index} missing required fields: {missing}")

    # Coordinate bounds
    lat = entry["latitude"]
    lon = entry["longitude"]
    if not (-90 <= lat <= 90):
        raise StationValidationError(
            f"Station {index} ({entry.get('station', '?')}): latitude {lat} out of range [-90, 90]"
        )
    if not (-180 <= lon <= 180):
        raise StationValidationError(
            f"Station {index} ({entry.get('station', '?')}): "
            f"longitude {lon} out of range [-180, 180]"
        )

    # Elevation sanity
    elev = entry["elevation_m"]
    if elev < -500 or elev > 9000:
        warnings.append(
            f"Station {index} ({entry.get('station', '?')}): elevation_m={elev} seems unusual"
        )

    # Channel pattern
    channels = entry.get("channels", "")
    if channels and channels not in VALID_CHANNEL_PATTERNS:
        warnings.append(
            f"Station {index} ({entry.get('station', '?')}): unusual channel pattern '{channels}'"
        )

    return warnings


def validate_stations(stations: list[dict[str, Any]]) -> list[str]:
    """Validate all station entries. Raises on critical errors; returns warnings."""
    if not isinstance(stations, list) or len(stations) == 0:
        raise StationValidationError("stations file must contain a non-empty JSON array")

    all_warnings: list[str] = []
    for i, s in enumerate(stations):
        all_warnings.extend(validate_station(s, i))
    return all_warnings
