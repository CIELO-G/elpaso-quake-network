"""Crash-safe file writers.

Both helpers write to a temp file in the same directory as the destination,
``fsync`` the temp file, then ``os.replace`` it onto the destination path.
``os.replace`` is atomic at the filesystem level, so a reader will always see
either the previous contents or the new contents — never a partial write.

The ``fsync`` is what protects against power loss / OS crash: ``os.replace``
guarantees ordering relative to other operations but does NOT guarantee the
data has reached disk. Without fsync, ``replace`` can rename a temp file
whose contents are still in the OS page cache, and a hard crash leaves an
empty file on disk.
"""

from __future__ import annotations

import csv
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

if TYPE_CHECKING:
    import pandas as pd


def atomic_write_df(
    df: pd.DataFrame,
    path: Path,
    *,
    index: bool = False,
    **to_csv_kwargs,
) -> None:
    """Write ``df`` to ``path`` as CSV via temp file + ``os.replace`` + fsync.

    Extra ``to_csv_kwargs`` are forwarded to ``df.to_csv``.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp", prefix=path.stem + "_")
    try:
        with os.fdopen(fd, "w", newline="") as f:
            df.to_csv(f, index=index, **to_csv_kwargs)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, str(path))
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def atomic_write_text(text: str, path: Path) -> None:
    """Write a string to ``path`` atomically (temp + fsync + replace)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp", prefix=path.stem + "_")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, str(path))
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


@contextmanager
def atomic_replace(path: Path) -> Iterator[Path]:
    """Yield a temp path next to ``path``; on success fsync + replace.

    For writers that need a *filename* (ObsPy ``Stream.write``, libraries
    that open the file themselves). On any exception the temp file is
    removed and ``path`` is left untouched.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp", prefix=path.stem + "_")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        yield tmp
        fd = os.open(str(tmp), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(str(tmp), str(path))
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def atomic_write_rows(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    """Write a list of dicts as CSV with a header, atomically."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp", prefix=path.stem + "_")
    try:
        with os.fdopen(fd, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, str(path))
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def atomic_write_stream(stream, path: Path, **write_kwargs) -> None:
    """Write an ObsPy Stream to ``path`` atomically (temp + fsync + replace)."""
    with atomic_replace(path) as tmp:
        stream.write(str(tmp), **write_kwargs)
