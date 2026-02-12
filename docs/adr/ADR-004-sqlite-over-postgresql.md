# ADR-004: SQLite Over PostgreSQL for Data Tracking

## Status

Accepted

## Context

The pipeline needs a database to track which waveform chunks have been downloaded, avoiding redundant FDSNWS requests. The database stores download metadata (network, station, time range, status, file paths) and is queried on every ingestion run.

Options considered:
- **SQLite**: Embedded, zero-configuration, single-file database.
- **PostgreSQL**: Full-featured client/server relational database.
- **Plain files** (JSON/CSV markers): File-based tracking using marker files or a manifest.

## Decision

We use SQLite for the download tracking database (`output/1-downloads.db`).

## Consequences

**Positive**:
- Zero configuration: no server to install, manage, or monitor. The database is a single file in the output directory.
- No additional dependencies: Python's `sqlite3` module is part of the standard library.
- Portable: the database file can be moved, backed up, or deleted trivially.
- WAL (Write-Ahead Logging) mode is enabled for safe concurrent reads during threaded ingestion.
- Thread-safe access is implemented with a threading lock for write operations.
- Sufficient performance: the download tracker handles thousands of records with sub-millisecond query times.
- Fits the single-node deployment model: the pipeline runs on one machine.

**Negative**:
- Not suitable for concurrent write access from multiple machines or processes (not a current requirement).
- No built-in replication or high availability.
- Limited to the download tracking use case; the event catalog is stored as CSV files rather than in SQLite.

## Alternatives Considered

- **PostgreSQL**: Would require installing and managing a database server, adding operational complexity disproportionate to the simple tracking use case. The pipeline runs on a single machine and does not need multi-user access, replication, or complex queries.
- **Plain files**: Using marker files (e.g., `.downloaded` flags) or a JSON manifest. Rejected because querying which chunks are downloaded would require scanning many files, and atomic updates are harder to guarantee than with SQLite transactions.
