"""Admin endpoints fired from the in-window menubar.

These shell out to local scripts / OS commands, so they're intentionally
narrow and local-only. Use POST so they don't fire from accidental link
prefetches and so they're easy to distinguish from read-only endpoints.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request

from dashboard.deps import LOGS_DIR, OUTPUT_DIR, ROOT
from dashboard.middleware import is_loopback

router = APIRouter()


def _require_local(request: Request) -> None:
    """Reject non-loopback callers.

    open-folder and quit act on the *server's* desktop session (Finder,
    the pywebview window) — they are meaningless and dangerous from a
    remote client, auth or not.
    """
    if not is_loopback(request):
        raise HTTPException(status_code=403, detail="Local-only endpoint")

_BACKUP_SCRIPT = ROOT / "scripts" / "backup.py"
_VALIDATE_SCRIPT = ROOT / "run_pipeline.py"

# Whitelist for the open-folder endpoint — never pass user-supplied paths
# to subprocess, only this fixed set.
_OPENABLE: dict[str, Path] = {
    "logs": LOGS_DIR,
    "output": OUTPUT_DIR,
    "root": ROOT,
    "readme": ROOT / "README.md",
}


def _run_capture(cmd: list[str], timeout: int = 60) -> dict:
    """Run a subprocess, capture stdout/stderr, return a JSON-shaped result."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "returncode": -1,
            "stdout": "",
            "stderr": f"Timed out after {timeout}s",
        }
    except FileNotFoundError as exc:
        return {"ok": False, "returncode": -1, "stdout": "", "stderr": str(exc)}
    return {
        "ok": result.returncode == 0,
        "returncode": result.returncode,
        "stdout": result.stdout or "",
        "stderr": result.stderr or "",
    }


@router.post("/api/admin/backup")
async def admin_backup():
    """Run scripts/backup.py. Returns stdout/stderr for in-window display."""
    if not _BACKUP_SCRIPT.exists():
        raise HTTPException(status_code=500, detail=f"backup.py not found at {_BACKUP_SCRIPT}")
    return _run_capture([sys.executable, str(_BACKUP_SCRIPT)], timeout=120)


@router.post("/api/admin/validate")
async def admin_validate():
    """Run run_pipeline.py --validate (env / FDSNWS / disk / imports check)."""
    if not _VALIDATE_SCRIPT.exists():
        raise HTTPException(
            status_code=500, detail=f"run_pipeline.py not found at {_VALIDATE_SCRIPT}"
        )
    return _run_capture(
        [sys.executable, str(_VALIDATE_SCRIPT), "--validate"],
        timeout=60,
    )


@router.post("/api/admin/open-folder")
async def admin_open_folder(
    request: Request, which: str = Query(..., pattern=r"^(logs|output|root|readme)$")
):
    """Open one of a fixed set of project paths in the OS default app."""
    _require_local(request)
    target = _OPENABLE.get(which)
    if target is None:
        raise HTTPException(status_code=400, detail=f"Unknown target: {which}")
    if not target.exists():
        raise HTTPException(status_code=404, detail=f"Path not found: {target}")
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", str(target)], check=False)
        elif sys.platform == "win32":
            os.startfile(str(target))  # type: ignore[attr-defined]
        else:
            subprocess.run(["xdg-open", str(target)], check=False)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to open: {exc}")
    return {"opened": str(target)}


@router.post("/api/admin/quit")
async def admin_quit(request: Request):
    """Terminate the dashboard process (also closes the pywebview window)."""
    _require_local(request)
    import threading

    # Defer the exit slightly so this response can flush before the process dies.
    threading.Timer(0.2, lambda: os._exit(0)).start()
    return {"quitting": True}
