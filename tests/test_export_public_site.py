"""scripts/export_public_site.py: retired event pages and the old-id map."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "export_public_site", ROOT / "scripts/export_public_site.py"
)
site = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(site)


def test_old_pages_go_and_old_ids_of_exported_events_map_to_new(tmp_path):
    events = tmp_path / "events"
    events.mkdir()
    for eid in ("ep20251122-0001", "ep20251122T183812", "ep20260101T000000"):
        (events / f"{eid}.json").write_text("{}")
    site.retire_old_pages(
        tmp_path,
        {"ep20251122T183812"},
        {"ep20251122-0001": "ep20251122T183812", "ep20251201-0000": "ep20251201T101010"},
    )
    assert sorted(p.stem for p in events.iterdir()) == ["ep20251122T183812"]
    aliases = json.loads((tmp_path / "aliases.json").read_text())
    assert aliases == {"ep20251122-0001": "ep20251122T183812"}  # unpublished old id not exposed
