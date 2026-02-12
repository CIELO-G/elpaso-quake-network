# ADR-005: FastAPI Over Django/Flask for the Dashboard

## Status

Accepted

## Context

The pipeline includes a monitoring dashboard that serves a web UI and provides API endpoints for real-time pipeline status, event catalog browsing, station health, disk usage, and more. The dashboard reads from the filesystem (JSON status files, CSV catalogs, log files) and serves this data as JSON to a single-page frontend.

Options considered:
- **FastAPI**: Modern Python ASGI framework with automatic API documentation, type hints, and async support.
- **Flask**: Mature WSGI micro-framework, widely used.
- **Django**: Full-featured web framework with ORM, admin, auth, etc.

## Decision

We chose FastAPI for the dashboard API server.

## Consequences

**Positive**:
- Async-native: FastAPI uses ASGI (via uvicorn), making it well-suited for the I/O-bound workload of reading files and serving JSON. All endpoint handlers are async.
- Automatic API documentation: FastAPI generates OpenAPI/Swagger docs at `/docs` (though the dashboard frontend does not use this, it aids development and debugging).
- Modern Python patterns: type hints, Pydantic models (available if needed), and clean decorator syntax.
- Lightweight: no ORM, template engine, or middleware stack. The dashboard has no database, no authentication, and no server-side rendering.
- Fast startup: uvicorn starts in under a second, important for the desktop-window launch mode (via pywebview).
- Built-in CORS, static file serving, and response type classes (`FileResponse`, `PlainTextResponse`).

**Negative**:
- Smaller ecosystem than Flask/Django for third-party extensions (not a concern for this simple dashboard).
- Async patterns may be unfamiliar to contributors used to synchronous Flask/Django.
- Requires uvicorn as an ASGI server (included in `environment.yml`).

## Alternatives Considered

- **Flask**: Mature and well-known. However, Flask is WSGI (synchronous) and would require additional setup for async file I/O. The simpler async model of FastAPI is a better fit for this I/O-heavy dashboard.
- **Django**: Vastly over-scoped for this use case. Django's ORM, admin interface, and template system add unnecessary complexity. The dashboard reads flat files, not a relational database, and has a single-page frontend with no server-side rendering.
