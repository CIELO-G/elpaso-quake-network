# Dashboard API Reference

The El Paso seismic pipeline dashboard is a FastAPI application serving a single-page frontend and a JSON API for pipeline monitoring. The server runs on `http://127.0.0.1:8000` by default.

All API endpoints return JSON unless otherwise noted. No authentication is required.

---

## GET /

**Description**: Serves the dashboard HTML page.

**Response**: `text/html` -- Single-page application with inline CSS and JavaScript.

---

## GET /api/status

**Description**: Returns the current pipeline execution status, including per-step progress. Data is read from `output/pipeline_status.json`, written by the pipeline orchestrator.

**Poll interval**: 2.5 seconds (in the frontend).

**Response schema**:

```json
{
  "pipeline": {
    "status": "running",
    "mode": "continuous",
    "started_at": "2026-01-15T02:00:00+00:00",
    "finished_at": null,
    "args": {
      "start": "2026-01-15",
      "end": "2026-01-15",
      "force": false,
      "debug": false,
      "start_step": 1,
      "end_step": 5
    },
    "continuous": {
      "current_day": "2026-01-15",
      "days_completed": 42,
      "days_skipped": [],
      "target_date": "2026-01-15",
      "waiting": false,
      "next_run_at": null,
      "day_times": [
        {
          "day": "2026-01-14",
          "total": 245.3,
          "steps": {
            "ingest": 120.5,
            "process": 45.2,
            "detect": 60.1,
            "associate": 15.3,
            "catalog": 4.2
          }
        }
      ]
    }
  },
  "steps": [
    {
      "number": 1,
      "name": "ingest",
      "status": "completed",
      "started_at": "2026-01-15T02:00:01+00:00",
      "finished_at": "2026-01-15T02:02:01+00:00",
      "return_code": 0,
      "log_file": "logs/ingest.log"
    }
  ]
}
```

**Field details**:
- `pipeline.status`: One of `"idle"`, `"running"`, `"completed"`, `"failed"`, `"waiting"`, `"stopped"`
- `pipeline.mode`: `"single"` or `"continuous"`
- `pipeline.continuous`: Present only in continuous mode
- `pipeline.continuous.waiting`: `true` when caught up and sleeping until the next day is eligible
- `steps[].status`: One of `"pending"`, `"running"`, `"completed"`, `"failed"`, `"skipped"`

**When pipeline is idle** (no status file):

```json
{
  "pipeline": {"status": "idle"},
  "steps": []
}
```

---

## GET /api/stations

**Description**: Returns the station list from `stations.json`. Cached in memory after first load.

**Poll interval**: Loaded once at dashboard startup.

**Response schema**:

```json
[
  {
    "network": "AM",
    "station": "R0F2D",
    "latitude": 31.648649,
    "longitude": -106.460311,
    "elevation_m": 1196.0,
    "model": "RS3D",
    "channels": "EH?"
  }
]
```

---

## GET /api/catalog

**Description**: Returns all events from the master catalog (`output/5-catalog/catalog.csv`).

**Poll interval**: 10 seconds.

**Response schema**:

```json
{
  "events": [
    {
      "event_id": "ep20260115-0003",
      "time": "2026-01-15T08:23:45.120000",
      "magnitude": 1.23,
      "latitude": 31.801234,
      "longitude": -106.227456,
      "depth_km": 8.5,
      "num_picks": 6
    }
  ]
}
```

**Notes**:
- `magnitude`, `latitude`, `longitude`, `depth_km`, `num_picks` are `null` if the source field is empty.

---

## GET /api/stats

**Description**: Returns summary statistics across the entire catalog and data archive.

**Poll interval**: 10 seconds.

**Response schema**:

```json
{
  "catalog_events": 127,
  "magnitude_min": -0.5,
  "magnitude_max": 3.2,
  "total_picks": 45230,
  "days_with_picks": 75,
  "days_with_events": 42,
  "days_with_raw": 80,
  "station_count": 11,
  "latest_event_time": "2026-01-15T08:23:45.120000",
  "latest_event_id": "ep20260115-0003"
}
```

**Notes**:
- `magnitude_min` and `magnitude_max` are `null` if no events have magnitudes.
- `days_with_raw` counts day directories with at least 3 miniSEED files (to exclude spillover directories).

---

## GET /api/progress

**Description**: Returns pipeline processing progress, comparing completed days against the target date.

**Poll interval**: 10 seconds.

**Response schema**:

```json
{
  "start_date": "2025-11-01",
  "target_date": "2026-01-15",
  "total_days": 76,
  "days_ingested": 75,
  "days_processed": 74,
  "days_detected": 74,
  "days_associated": 72,
  "days_complete": 72,
  "percent": 94.7,
  "current_day": "2026-01-14",
  "pipeline_status": "running",
  "mode": "continuous",
  "waiting": false,
  "next_run_at": null,
  "days_completed_continuous": 72,
  "days_skipped": []
}
```

**Field details**:
- `start_date`: Fixed pipeline start date (2025-11-01)
- `target_date`: Current date minus 6-hour lag
- `total_days`: Number of days from start to target
- `days_complete`: Minimum of days across all 4 processing stages
- `percent`: `days_complete / total_days * 100`
- `days_ingested`: Day directories in `1-raw/` with at least 3 files
- `days_processed`: Day directories in `2-processed/`
- `days_detected`: Days with non-empty picks CSVs in `3-picks/`
- `days_associated`: Days with non-empty event CSVs in `4-events/`

---

## GET /api/throughput

**Description**: Returns per-day processing time averages and ETA, sourced from continuous-mode timing data.

**Poll interval**: 10 seconds.

**Response schema**:

```json
{
  "avg_day_sec": 245.3,
  "last_day_sec": 220.1,
  "step_avg_sec": {
    "ingest": 120.5,
    "process": 45.2,
    "detect": 60.1,
    "associate": 15.3,
    "catalog": 4.2
  },
  "remaining_days": 4,
  "eta_sec": 981,
  "sample_size": 42
}
```

**Notes**:
- All fields are `null` and `remaining_days` is 0 when no continuous-mode timing data is available.
- `sample_size`: Number of day timings used for averaging (max 100, most recent kept).

---

## GET /api/errors

**Description**: Returns recent errors, warnings, and critical messages parsed from pipeline log files and the pipeline status file.

**Query parameters**:

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `limit` | int | 50 | Maximum number of entries to return |

**Poll interval**: 10 seconds.

**Example request**: `GET /api/errors?limit=20`

**Response schema**:

```json
{
  "errors": [
    {
      "timestamp": "2026-01-15 08:23:45",
      "level": "ERROR",
      "step": "ingest",
      "message": "Attempt 3/4 failed for AM.R0F2D (timeout). Retrying in 40s..."
    },
    {
      "timestamp": "2026-01-15T08:25:00+00:00",
      "level": "CRITICAL",
      "step": "detect",
      "message": "Step detect failed (exit code 1)"
    }
  ]
}
```

**Field details**:
- `level`: One of `"ERROR"`, `"CRITICAL"`, `"WARNING"`
- `step`: One of `"ingest"`, `"process"`, `"detect"`, `"associate"`, `"catalog"`
- Entries are sorted by timestamp descending (most recent first)
- Includes both log-file entries and failed-step entries from `pipeline_status.json`

---

## GET /api/disk

**Description**: Returns filesystem disk usage and per-stage output directory sizes.

**Poll interval**: 30 seconds.

**Response schema**:

```json
{
  "total_gb": 500.0,
  "used_gb": 120.5,
  "free_gb": 379.5,
  "usage_percent": 24.1,
  "output_size_gb": 2.45,
  "breakdown": {
    "1-raw": 1.234,
    "1-metadata": 0.001,
    "2-processed": 1.100,
    "3-picks": 0.005,
    "4-events": 0.003,
    "5-catalog": 0.001,
    "1-downloads.db": 0.010
  }
}
```

**Notes**:
- `breakdown` values are in GB, rounded to 3 decimal places.
- `output_size_gb` is the sum of all breakdown values.

---

## GET /api/station_health

**Description**: Checks which stations have raw data files in the most recent 3 day directories. Used to color-code stations on the map.

**Poll interval**: 30 seconds.

**Response schema**:

```json
[
  {
    "station": "R0F2D",
    "network": "AM",
    "status": "ok",
    "files_recent": 3
  },
  {
    "station": "R5912",
    "network": "AM",
    "status": "warning",
    "files_recent": 1
  }
]
```

**Field details**:
- `status`: `"ok"` (data in all 3 recent days), `"warning"` (data in some), `"error"` (no data), `"unknown"` (no recent day directories found)
- `files_recent`: Number of recent day directories (out of 3) where the station has miniSEED files

---

## GET /api/event_rate

**Description**: Returns the count of catalog events per day, for the timeline bar chart.

**Poll interval**: 30 seconds.

**Response schema**:

```json
{
  "days": [
    {"date": "2026-01-01", "count": 3},
    {"date": "2026-01-02", "count": 1},
    {"date": "2026-01-05", "count": 7}
  ]
}
```

**Notes**:
- Only days with at least one event are included (sparse).
- Sorted chronologically.

---

## GET /api/data_completeness

**Description**: Returns a matrix of raw miniSEED file counts per station per day. Used for the data completeness heatmap.

**Poll interval**: 30 seconds.

**Response schema**:

```json
{
  "stations": ["R0F2D", "R3E95", "R4B41"],
  "days": ["2026-01-01", "2026-01-02", "2026-01-03"],
  "matrix": [
    [3, 3, 3],
    [3, 0, 3],
    [3, 3, 3]
  ]
}
```

**Field details**:
- `stations`: Station codes in the order defined by `stations.json`
- `days`: ISO date strings, sorted chronologically
- `matrix[i][j]`: Number of miniSEED files for `stations[i]` on `days[j]`

---

## GET /api/pick_quality

**Description**: Returns a histogram of PhaseNet pick probabilities and P/S phase counts.

**Poll interval**: 30 seconds.

**Response schema**:

```json
{
  "bins": [0, 15, 42, 89, 234, 567, 890, 1200, 1500, 3000],
  "bin_edges": [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0],
  "total": 7537,
  "p_count": 4200,
  "s_count": 3337
}
```

**Field details**:
- `bins`: 10 bins covering [0.0-0.1), [0.1-0.2), ..., [0.9-1.0]
- `bin_edges`: 11 edge values for the 10 bins
- `total`: Total number of picks across all CSV files
- `p_count` / `s_count`: Number of P-wave and S-wave picks

---

## GET /api/station_picks

**Description**: Returns a matrix of pick counts per station per day. Used for the station activity heatmap.

**Poll interval**: 30 seconds.

**Response schema**:

```json
{
  "stations": ["R0F2D", "R3E95", "R4B41"],
  "days": ["2026-01-01", "2026-01-02"],
  "matrix": [
    [25, 30],
    [12, 0],
    [45, 22]
  ]
}
```

**Field details**:
- `stations`: Station codes in the order defined by `stations.json`
- `days`: ISO date strings, sorted chronologically
- `matrix[i][j]`: Number of picks for `stations[i]` on `days[j]`

---

## GET /api/logs/{step_name}

**Description**: Returns the tail of a step's log file.

**Path parameters**:

| Parameter | Type | Description |
|-----------|------|-------------|
| `step_name` | string | One of: `ingest`, `process`, `detect`, `associate`, `catalog` |

**Query parameters**:

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `lines` | int | 100 | Number of lines to return from the end of the file |

**Response**: `text/plain` -- The last N lines of the log file.

**Example request**: `GET /api/logs/detect?lines=50`

**Error responses**:
- `400 Bad Request`: `"Unknown step: {step_name}"` -- if step_name is not in the valid set
- `200 OK` with `"No log file for {step_name}"` -- if the log file does not exist yet
