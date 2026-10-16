"""Rename positional event ids to time-based ids (one-off, 2026-10-16).

Ids issued before 2026-10-16 were positional, {prefix}{YYYYMMDD}-{NNNN}; new
events get {prefix}{YYYYMMDD}T{HHMMSS} (lib/event_ids.py). This gives every
existing event an id in the new form, minted from its current origin time
(oldest first, so a second event in the same second gets -2):

  5-catalog/catalog.csv, assignments.csv  event_id renamed; every other byte kept
  5-catalog/id_aliases.csv                legacy_id -> event_id, for old notes/links
  5-catalog/ingested_days.json            written from the positional ids first, if
                                          missing: the new ids carry origin dates,
                                          not the day file an event came from
  5-catalog/reviews.jsonl                 one appended "id_migration" record (with
                                          legacy_id) per event that has review
                                          history; existing lines untouched
  templates/                              each template renamed to the id of the
                                          event it was cut from, matched by origin
                                          time (the old names drifted: 8 of 34 named
                                          a duplicate or a different event); the
                                          archive, manifest.json, detections.csv and
                                          new_candidates.csv follow, old -> new in
                                          templates/id_aliases.csv

Originals are copied to id-migration-backup-<stamp>/ beside each file first.
Holds the catalog lock: run while the pipeline is waiting. Running it again
changes nothing.

    python scripts/migrate_event_ids.py            # dry run: print the plan
    python scripts/migrate_event_ids.py --apply
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
import tempfile
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib.atomic_io import atomic_replace, atomic_write_text  # noqa: E402
from lib.catalog_lock import catalog_lock  # noqa: E402
from lib.event_ids import (  # noqa: E402
    ALIAS_COLUMNS,
    ALIASES_FILENAME,
    is_legacy_id,
    mint_event_id,
    read_aliases,
)
from lib.ingested_days import (  # noqa: E402
    INGESTED_DAYS_FILENAME,
    days_from_ids,
    write_ingested_days,
)
from lib.reviews import SIDECAR_FILENAME, append_review_records, latest_records  # noqa: E402

TEMPLATE_MATCH_S = 10.0  # template origin vs catalog origin; observed <= 1.6 s
TEMPLATE_ARCHIVE = "blast_templates.tgz"


def utc(t: str | datetime) -> datetime:
    """Aware UTC datetime from an ISO string (naive = UTC) or datetime."""
    d = t if isinstance(t, datetime) else datetime.fromisoformat(str(t).replace("Z", "+00:00"))
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)


# ── planning (pure) ───────────────────────────────────────────────────


def plan_event_ids(events: list[dict]) -> dict[str, str]:
    """legacy id -> new id for every positional id in `events` (catalog rows)."""
    ids = [e["event_id"] for e in events]
    if len(set(ids)) != len(ids):
        raise ValueError("catalog has duplicate event ids; fix those first")
    taken = {i for i in ids if not is_legacy_id(i)}
    legacy = sorted(
        (e for e in events if is_legacy_id(e["event_id"])),
        key=lambda e: (utc(e["time"]), e["event_id"]),
    )
    return {e["event_id"]: mint_event_id(e["time"], e["event_id"][:2], taken) for e in legacy}


def plan_template_names(
    templates: list[tuple[str, datetime]],
    events: list[tuple[str, datetime]],
    aliases: dict[str, str],
) -> dict[str, str]:
    """Template name -> id of the event it was cut from (closest origin, same prefix).

    A second template of the same event is named {event_id}_2. The template
    whose old name already pointed at that event keeps the plain name.
    Raises ValueError if a template matches no event.
    """
    matched: dict[str, str] = {}
    unmatched = []
    for name, origin in templates:
        near = [
            (abs((t - origin).total_seconds()), eid)
            for eid, t in events
            if eid[:2] == name[:2] and abs((t - origin).total_seconds()) <= TEMPLATE_MATCH_S
        ]
        if near:
            matched[name] = min(near)[1]
        else:
            unmatched.append(name)
    if unmatched:
        raise ValueError(
            f"templates with no catalog event within {TEMPLATE_MATCH_S:g} s: {unmatched}"
        )

    def named_for_its_event(name: str) -> bool:
        return aliases.get(name, name) == matched[name]

    renamed: dict[str, str] = {}
    per_event: dict[str, int] = {}
    for name in sorted(matched, key=lambda n: (not named_for_its_event(n), n)):
        eid = matched[name]
        per_event[eid] = per_event.get(eid, 0) + 1
        renamed[name] = eid if per_event[eid] == 1 else f"{eid}_{per_event[eid]}"
    return renamed


# ── file helpers ──────────────────────────────────────────────────────


def read_csv_rows(path: Path) -> tuple[list[str], list[dict]]:
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        return list(reader.fieldnames or []), list(reader)


def write_csv_rows(path: Path, columns: list[str], rows: Iterable[dict]) -> None:
    """Atomic CSV write; '\\n' line ends and minimal quoting, as pandas writes."""
    with atomic_replace(path) as tmp, open(tmp, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def rename_column(path: Path, column: str, mapping: dict[str, str]) -> int:
    """Rewrite `column` of a CSV through `mapping`, streaming; returns rows changed."""
    changed = 0
    with atomic_replace(path) as tmp:
        with open(path, newline="") as src, open(tmp, "w", newline="") as dst:
            reader = csv.reader(src)
            writer = csv.writer(dst, lineterminator="\n")
            header = next(reader)
            writer.writerow(header)
            k = header.index(column)
            for row in reader:
                if row[k] in mapping:
                    row[k] = mapping[row[k]]
                    changed += 1
                writer.writerow(row)
    return changed


def backup(files: list[Path], stamp: str) -> Path:
    dest = files[0].parent / f"id-migration-backup-{stamp}"
    dest.mkdir()
    for f in files:
        if f.exists():
            shutil.copy2(f, dest / f.name)
    return dest


def write_aliases(path: Path, mapping: dict[str, str]) -> None:
    merged = {**read_aliases(path), **mapping}
    write_csv_rows(
        path, ALIAS_COLUMNS, ({"legacy_id": k, "event_id": v} for k, v in sorted(merged.items()))
    )


# ── the two migrations ────────────────────────────────────────────────


def migrate_catalog(catalog_dir: Path, apply: bool, stamp: str) -> dict[str, str]:
    """Rename catalog ids; returns legacy -> new (empty when nothing to do)."""
    catalog_file = catalog_dir / "catalog.csv"
    assignments_file = catalog_dir / "assignments.csv"
    sidecar = catalog_dir / SIDECAR_FILENAME
    with catalog_lock(catalog_dir, timeout=60):
        columns, events = read_csv_rows(catalog_file)
        id_map = plan_event_ids(events)
        print(f"catalog: {len(events)} events, {len(id_map)} positional ids to rename")
        if not id_map:
            return {}
        collisions = sorted(v for v in id_map.values() if "-" in v[2:])
        print(f"  same-second collisions (suffixed): {collisions or 'none'}")
        for old, new in list(id_map.items())[:5]:
            print(f"  {old} -> {new}")
        print("  ...")
        if not apply:
            return id_map

        print(f"  backup: {backup([catalog_file, assignments_file, sidecar], stamp)}")
        ledger = catalog_dir / INGESTED_DAYS_FILENAME
        if not ledger.exists():
            write_ingested_days(ledger, days_from_ids(e["event_id"] for e in events))
            print(f"  wrote {ledger.name} from the positional ids")
        for e in events:
            e["event_id"] = id_map.get(e["event_id"], e["event_id"])
        write_csv_rows(catalog_file, columns, events)
        n_picks = rename_column(assignments_file, "event_id", id_map)
        write_aliases(catalog_dir / ALIASES_FILENAME, id_map)

        legacy_of = {v: k for k, v in id_map.items()}
        history = latest_records(sidecar)
        reviewed = {
            e["event_id"]: {**e, "legacy_id": legacy_of[e["event_id"]]}
            for e in events
            if e["event_id"] in legacy_of and legacy_of[e["event_id"]] in history
        }
        _, picks = read_csv_rows(assignments_file)
        by_event: dict[str, list[dict]] = {}
        for p in picks:
            if p["event_id"] in reviewed:
                by_event.setdefault(p["event_id"], []).append(p)
        n_reviews = append_review_records(sidecar, reviewed, by_event, source="id_migration")
        print(
            f"  renamed {len(id_map)} events, {n_picks} picks; "
            f"{n_reviews} review records appended; aliases -> {ALIASES_FILENAME}"
        )
    return id_map


def migrate_templates(templates_dir: Path, catalog_dir: Path, apply: bool, stamp: str) -> None:
    archive = templates_dir / TEMPLATE_ARCHIVE
    if not archive.exists():
        print("templates: none")
        return
    from eqcorrscan.core.match_filter import Tribe  # heavy; only needed here

    tribe = Tribe().read(str(archive))
    if not any(is_legacy_id(t.name) for t in tribe):
        print(f"templates: {len(tribe)}, all already renamed")
        return
    templates = [(t.name, utc(t.event.origins[0].time.datetime)) for t in tribe]
    _, events = read_csv_rows(catalog_dir / "catalog.csv")
    aliases = read_aliases(catalog_dir / ALIASES_FILENAME)
    if apply:
        current = [(e["event_id"], utc(e["time"])) for e in events]
    else:  # catalog not renamed yet: match against the ids it would get
        planned = plan_event_ids(events)
        current = [(planned.get(e["event_id"], e["event_id"]), utc(e["time"])) for e in events]
        aliases = {**aliases, **planned}
    names = plan_template_names(templates, current, aliases)
    drifted = sorted(n for n in names if aliases.get(n, n) != names[n])
    print(f"templates: {len(names)} to rename; old name not this event's id: {len(drifted)}")
    for n in drifted:
        print(f"  {n} -> {names[n]}")
    if not apply:
        return

    files = [archive, *(templates_dir / f for f in ("manifest.json", "detections.csv",
                                                    "new_candidates.csv"))]  # fmt: skip
    print(f"  backup: {backup(files, stamp)}")
    for t in tribe:
        t.name = names[t.name]
    work = Path(tempfile.mkdtemp(dir=templates_dir, prefix=".id-migration-"))
    try:
        tribe.write(str(work / archive.stem))  # EQcorrscan appends .tgz
        written = Tribe().read(str(work / archive.name))
        if sorted(t.name for t in written) != sorted(names.values()):
            raise RuntimeError("rewritten template archive does not read back")
        (work / archive.name).replace(archive)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    manifest_file = templates_dir / "manifest.json"
    if manifest_file.exists():
        manifest = json.loads(manifest_file.read_text())
        for entry in manifest["templates"]:
            old = entry["id"]
            entry.update(id=names[old], event_id=names[old].split("_")[0], legacy_id=old)
        atomic_write_text(json.dumps(manifest, indent=2), manifest_file)
    n_det = n_cand = 0
    if (templates_dir / "detections.csv").exists():
        n_det = rename_column(templates_dir / "detections.csv", "template_id", names)
    if (templates_dir / "new_candidates.csv").exists():
        n_cand = rename_column(templates_dir / "new_candidates.csv", "best_template", names)
    write_aliases(templates_dir / ALIASES_FILENAME, names)
    print(f"  renamed {len(names)} templates, {n_det} detections, {n_cand} candidates")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--output-dir", type=Path, default=ROOT / "output")
    ap.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    args = ap.parse_args(argv)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    catalog_dir = args.output_dir / "5-catalog"
    migrate_catalog(catalog_dir, args.apply, stamp)
    migrate_templates(args.output_dir / "templates", catalog_dir, args.apply, stamp)
    if not args.apply:
        print("DRY RUN: nothing written. Re-run with --apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
