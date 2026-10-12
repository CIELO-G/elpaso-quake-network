"""Data models for the El Paso seismic pipeline.

Provides Pydantic models for configuration validation and dataclasses
for runtime data structures (Pick, Event, Station).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
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
    end_date: str = ""  # last day the station is expected to have data ("" = active)
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
            end_date=d.get("end_date", ""),
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

    # Inside the study area (B5): a station outside the box breaks NLLoc
    # Grid2Time and is invisible on the dashboard map.
    from lib.geometry import inside_study_area, study_area  # local: avoid import cycle

    if not inside_study_area(lat, lon):
        box = study_area()
        raise StationValidationError(
            f"Station {index} ({entry.get('station', '?')}): ({lat}, {lon}) is outside the "
            f"study area lat {box['lat_min']:.2f}..{box['lat_max']:.2f}, "
            f"lon {box['lon_min']:.2f}..{box['lon_max']:.2f} (lib/constants.py)"
        )

    # Instrument block: detection needs a velocity sensor with a sensitivity
    try:
        velocity_sensor(entry)
        channel_layout(entry)
    except StationValidationError as exc:
        raise StationValidationError(f"Station {index}: {exc}") from None

    # Dates: start_date is required by the orchestrator; end_date marks retirement
    name = entry.get("station", "?")
    start = _parse_iso_date(entry.get("start_date"), f"Station {index} ({name}): start_date")
    if start is None:
        raise StationValidationError(f"Station {index} ({name}): start_date is required (YYYY-MM-DD)")
    if "end_date" in entry and entry["end_date"] not in ("", None):
        end = _parse_iso_date(entry["end_date"], f"Station {index} ({name}): end_date")
        if end < start:
            raise StationValidationError(
                f"Station {index} ({name}): end_date {end} is before start_date {start}"
            )

    return warnings


def _parse_iso_date(value: Any, what: str) -> date | None:
    if value in ("", None):
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise StationValidationError(f"{what} must be YYYY-MM-DD, got {value!r}") from exc


# Sensor kinds that record ground velocity, in preference order. PhaseNet
# and ML both want velocity, so an RS4D's accelerometer is never chosen.
VELOCITY_SENSOR_KINDS = ("geophone", "seismometer", "broadband")


def velocity_sensor(entry: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Return ``(kind, spec)`` for the station's velocity sensor.

    ``spec`` always has a positive numeric ``sensitivity`` (counts per m/s).
    Raises StationValidationError with the station name when the entry has no
    usable instrument block, so a mistyped new station fails at load time
    instead of as a KeyError in the middle of detection (review item B2).
    """
    name = entry.get("station", "?")
    inst = entry.get("instrument")
    if not isinstance(inst, dict) or not inst:
        raise StationValidationError(f"({name}): 'instrument' must be a non-empty object")
    kind = next((k for k in VELOCITY_SENSOR_KINDS if k in inst), None)
    if kind is None:
        raise StationValidationError(
            f"({name}): instrument has no velocity sensor; expected one of "
            f"{', '.join(VELOCITY_SENSOR_KINDS)}, got {', '.join(inst)}"
        )
    spec = inst[kind]
    sens = spec.get("sensitivity") if isinstance(spec, dict) else None
    if not isinstance(sens, (int, float)) or isinstance(sens, bool) or sens <= 0:
        raise StationValidationError(
            f"({name}): instrument.{kind}.sensitivity must be a positive number, got {sens!r}"
        )
    return kind, spec


def channel_layout(entry: dict[str, Any]) -> tuple[str, list[str]]:
    """Return ``(band_prefix, components)`` for the channels detection reads.

    Derived from the station's ``channels`` pattern and model so a new
    station type (e.g. RS1D, vertical only) cannot silently be treated as
    three-component:

    * ``HH?``            -> ("HH", [E, N, Z])   broadband
    * ``EH?``            -> ("EH", [E, N, Z])   RS3D
    * ``EHZ`` / RS1D     -> ("EH", [Z])
    * ``E??`` / RS4D     -> ("EH", [Z])         geophone only; EN? accelerometer ignored
    """
    name = entry.get("station", "?")
    chans = str(entry.get("channels", "") or "EHZ")  # Station dataclass default
    model = str(entry.get("model", "") or "").upper()
    if chans == "HH?":
        return "HH", ["E", "N", "Z"]
    if chans == "EH?":
        return "EH", ["E", "N", "Z"]
    if chans == "EHZ" or model == "RS1D":
        return "EH", ["Z"]
    if chans == "E??" or model == "RS4D":
        return "EH", ["Z"]
    if chans.startswith("EH"):
        return "EH", ["E", "N", "Z"]
    raise StationValidationError(
        f"({name}): cannot derive channel layout from channels={chans!r} model={model!r}"
    )


def station_active_on(entry: dict[str, Any], day: date) -> bool:
    """True when ``day`` lies within the station's [start_date, end_date] window.

    ``end_date`` is inclusive and optional; a missing/empty one means the
    station is still active. A retired station keeps its history (old days
    still re-process with its data) but is no longer expected, fetched, or
    alerted on after ``end_date``.
    """
    start = _parse_iso_date(entry.get("start_date"), "start_date")
    if start is not None and day < start:
        return False
    end = _parse_iso_date(entry.get("end_date"), "end_date")
    if end is not None and day > end:
        return False
    return True


def validate_stations(stations: list[dict[str, Any]]) -> list[str]:
    """Validate all station entries. Raises on critical errors; returns warnings."""
    if not isinstance(stations, list) or len(stations) == 0:
        raise StationValidationError("stations file must contain a non-empty JSON array")

    all_warnings: list[str] = []
    for i, s in enumerate(stations):
        all_warnings.extend(validate_station(s, i))
    return all_warnings
