#!/usr/bin/env python3
"""Build dashboard/static/quarries.geojson from MSHA + OpenStreetMap.

Two sources:
  - MSHA Mine Data Retrieval System (US only, authoritative, ~quarterly)
  - OpenStreetMap via Overpass API (worldwide, picks up Chihuahua/Mexico
    + small local quarries MSHA may miss)

Filters to:
  - the El Paso / southern Rio Grande Rift / northern Chihuahua bbox
  - active (or intermittent) status for MSHA
  - non-coal mines only

Rerun whenever MSHA publishes new data, or to refresh OSM. The OSM fetch
takes ~5-15 s and needs internet; skip it with --no-osm if offline.

Usage
-----
    python scripts/build_quarries_geojson.py
    python scripts/build_quarries_geojson.py --no-osm

Input  : data/msha/Mines.txt           (pipe-delimited)
Output : dashboard/static/quarries.geojson
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INPUT = ROOT / "data" / "msha" / "Mines.txt"
OUTPUT = ROOT / "dashboard" / "static" / "quarries.geojson"

# Bounding box: El Paso + Las Cruces + Carlsbad + Vado/Anthony belt +
# northern Chihuahua (down to ~55 km below border for Mexico coverage).
BBOX = {
    "lat_min": 30.5,
    "lat_max": 32.5,
    "lon_min": -107.0,
    "lon_max": -104.0,
}

# Statuses we treat as "could blast / could be a seismic source".
# MSHA uses these labels in CURRENT_MINE_STATUS:
ACTIVE_STATUSES = {"Active", "Intermittent", "NonProducing Active"}

# Overpass API public endpoint (try a couple if one's overloaded)
OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

# Deduplication radius — if an OSM quarry is within this many degrees of an
# MSHA mine (~500m at this latitude), prefer MSHA's better metadata.
DEDUP_DEG = 0.005


def load_msha() -> list[dict]:
    """Parse MSHA Mines.txt → list of GeoJSON Feature dicts."""
    if not INPUT.exists():
        sys.stderr.write(f"ERROR: MSHA input not found at {INPUT}\n")
        sys.stderr.write(
            "       Download Mines.zip from\n"
            "       https://arlweb.msha.gov/OpenGovernmentData/DataSets/Mines.zip\n"
            "       and extract Mines.txt into data/msha/.\n"
        )
        return []

    features: list[dict] = []
    n_total = n_bbox = n_active = n_kept = 0

    with INPUT.open(encoding="latin-1", newline="") as f:
        reader = csv.DictReader(f, delimiter="|")
        for row in reader:
            n_total += 1
            try:
                lat = float(row["LATITUDE"])
                lon = float(row["LONGITUDE"])
            except (ValueError, KeyError):
                continue
            if not (BBOX["lat_min"] <= lat <= BBOX["lat_max"]):
                continue
            if not (BBOX["lon_min"] <= lon <= BBOX["lon_max"]):
                continue
            n_bbox += 1
            status = (row.get("CURRENT_MINE_STATUS") or "").strip()
            if status not in ACTIVE_STATUSES:
                continue
            n_active += 1
            if (row.get("COAL_METAL_IND") or "").strip() == "C":
                continue
            n_kept += 1
            features.append(
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [lon, lat]},
                    "properties": {
                        "mine_id": (row.get("MINE_ID") or "").strip(),
                        "name": (row.get("CURRENT_MINE_NAME") or "Unknown").strip(),
                        "operator": (row.get("CURRENT_OPERATOR_NAME") or "").strip(),
                        "type": (row.get("CURRENT_MINE_TYPE") or "").strip(),
                        "status": status,
                        "commodity": (row.get("PRIMARY_CANVASS") or "").strip() or "Unknown",
                        "state": (row.get("STATE") or "").strip(),
                        "county": (row.get("FIPS_CNTY_NM") or "").strip(),
                        "source": "MSHA",
                    },
                }
            )
    print(f"MSHA: {n_total:,} total → {n_bbox} in bbox → {n_active} active → {n_kept} kept")
    return features


def load_osm() -> list[dict]:
    """Query OSM Overpass for landuse=quarry / industrial=mine in bbox."""
    bbox_str = f"{BBOX['lat_min']},{BBOX['lon_min']},{BBOX['lat_max']},{BBOX['lon_max']}"
    query = f"""
    [out:json][timeout:30];
    (
      way["landuse"="quarry"]({bbox_str});
      node["landuse"="quarry"]({bbox_str});
      relation["landuse"="quarry"]({bbox_str});
      way["industrial"="mine"]({bbox_str});
      node["industrial"="mine"]({bbox_str});
    );
    out center tags;
    """.strip()

    data = None
    last_err = None
    for url in OVERPASS_ENDPOINTS:
        try:
            print(f"OSM: querying Overpass ({url}) …")
            req = urllib.request.Request(
                url,
                data=query.encode("utf-8"),
                headers={"User-Agent": "elpaso-quake-network/quarry-builder"},
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.load(resp)
                break
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            last_err = exc
            print(f"  failed: {exc}")
            continue
    if data is None:
        sys.stderr.write(f"OSM fetch failed (last error: {last_err})\n")
        return []

    features: list[dict] = []
    for elem in data.get("elements", []):
        # Nodes have lat/lon directly; ways/relations use the 'center' from out center
        if elem["type"] == "node":
            lat, lon = elem.get("lat"), elem.get("lon")
        else:
            ctr = elem.get("center", {})
            lat, lon = ctr.get("lat"), ctr.get("lon")
        if lat is None or lon is None:
            continue
        tags = elem.get("tags", {})
        # Infer commodity from OSM tags when possible
        resource = tags.get("resource") or tags.get("mineral") or tags.get("rock") or ""
        if "stone" in resource.lower() or tags.get("landuse") == "quarry":
            commodity = "Stone"  # default for landuse=quarry
        elif "sand" in resource.lower() or "gravel" in resource.lower():
            commodity = "SandAndGravel"
        elif tags.get("industrial") == "mine":
            commodity = "Mine"
        else:
            commodity = resource.capitalize() if resource else "Unknown"

        # Crude US/MX split by longitude+latitude: anything south of 31.78 is MX
        in_mexico = lat < 31.78
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": {
                    "mine_id": f"osm{elem['type'][0]}{elem.get('id', '')}",
                    "name": tags.get("name") or tags.get("operator") or "Unnamed (OSM)",
                    "operator": tags.get("operator", ""),
                    "type": tags.get("landuse") or tags.get("industrial") or "",
                    "status": "OSM-tagged",
                    "commodity": commodity,
                    "state": "Chihuahua" if in_mexico else "",
                    "county": tags.get("addr:state", ""),
                    "source": "OSM",
                },
            }
        )
    print(f"OSM: {len(features)} features in bbox")
    return features


def dedup(features: list[dict]) -> list[dict]:
    """Remove OSM features within DEDUP_DEG of an MSHA feature (MSHA wins)."""
    msha = [f for f in features if f["properties"]["source"] == "MSHA"]
    osm = [f for f in features if f["properties"]["source"] == "OSM"]
    kept_osm = []
    for o in osm:
        olon, olat = o["geometry"]["coordinates"]
        too_close = False
        for m in msha:
            mlon, mlat = m["geometry"]["coordinates"]
            if abs(olon - mlon) < DEDUP_DEG and abs(olat - mlat) < DEDUP_DEG:
                too_close = True
                break
        if not too_close:
            kept_osm.append(o)
    dropped = len(osm) - len(kept_osm)
    if dropped:
        print(f"Deduped: dropped {dropped} OSM features within {DEDUP_DEG}° of MSHA")
    return msha + kept_osm


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-osm",
        action="store_true",
        help="Skip OSM/Overpass fetch (MSHA only)",
    )
    args = parser.parse_args()

    features = load_msha()
    if not args.no_osm:
        features += load_osm()
        features = dedup(features)

    geojson = {
        "type": "FeatureCollection",
        "features": features,
        "_meta": {
            "sources": ["MSHA"] if args.no_osm else ["MSHA", "OSM Overpass"],
            "bbox": BBOX,
            "active_statuses_msha": sorted(ACTIVE_STATUSES),
            "total_features": len(features),
        },
    }

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(geojson, indent=2))
    print(f"\nWrote {OUTPUT.relative_to(ROOT)} ({len(features)} features)")

    by_src = Counter(f["properties"]["source"] for f in features)
    print(f"By source: {dict(by_src)}")
    by_comm = Counter(f["properties"]["commodity"] for f in features)
    print("By commodity:")
    for comm, n in by_comm.most_common():
        print(f"  {n:4}  {comm}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
