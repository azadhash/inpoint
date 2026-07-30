"""Match files Chrome dropped into ~/Downloads against an expected manifest, then file them."""

from __future__ import annotations

import glob as globlib
import shutil
import time
from pathlib import Path
from typing import Callable, Iterable

from .paths import unique_path

PARTIAL_SUFFIXES = (".crdownload", ".download", ".part", ".tmp")


def now() -> float:
    return time.time()


def _partials(downloads_dir: Path, since: float) -> list[Path]:
    out = []
    for suffix in PARTIAL_SUFFIXES:
        for p in downloads_dir.glob(f"*{suffix}"):
            try:
                if p.stat().st_mtime >= since - 30:
                    out.append(p)
            except OSError:
                continue
    return out


def _variants(filename: str) -> list[str]:
    """Chrome renames collisions to 'name (1).ext'; some sites hand back 'name-1.ext'."""
    path = Path(filename)
    stem, ext = path.stem, path.suffix
    esc_stem, esc_ext = globlib.escape(stem), globlib.escape(ext)
    return [
        globlib.escape(filename),
        f"{esc_stem} (*){esc_ext}",
        f"{esc_stem}-[0-9]{esc_ext}",
        f"{esc_stem}*{esc_ext}",
    ]


def find_download(
    expected: dict,
    downloads_dir: Path,
    since: float,
    *,
    claimed: set[Path] | None = None,
) -> Path | None:
    """Best match in `downloads_dir` for one expected attachment."""
    claimed = claimed or set()
    filename = expected["filename"]
    size = int(expected.get("size") or 0)

    def usable(p: Path) -> bool:
        if p in claimed or not p.is_file() or p.suffix.lower() in PARTIAL_SUFFIXES:
            return False
        try:
            return p.stat().st_mtime >= since - 30
        except OSError:
            return False

    for pattern in _variants(filename):
        hits = [p for p in downloads_dir.glob(pattern) if usable(p)]
        if hits:
            # Prefer an exact size match, then the most recent.
            if size:
                exact = [p for p in hits if abs(p.stat().st_size - size) <= max(64, size * 0.01)]
                if exact:
                    hits = exact
            return max(hits, key=lambda p: p.stat().st_mtime)

    # Last resort: right extension, right size, arrived after we started.
    if size:
        ext = Path(filename).suffix.lower()
        loose = [
            p for p in downloads_dir.iterdir()
            if usable(p) and p.suffix.lower() == ext
            and abs(p.stat().st_size - size) <= max(64, size * 0.01)
        ]
        if loose:
            return max(loose, key=lambda p: p.stat().st_mtime)
    return None


def collect(
    expected: Iterable[dict],
    downloads_dir: Path,
    *,
    since: float,
    timeout: float = 300.0,
    poll: float = 1.0,
    on_event: Callable[[str, str], None] | None = None,
) -> dict:
    """Wait for each expected file to land, then move it to its target path.

    Each `expected` entry needs: filename, size, target (str/Path of the final file).
    Returns {"moved": [...], "missing": [...]}.
    """
    downloads_dir = Path(downloads_dir)
    pending = list(expected)
    moved: list[dict] = []
    claimed: set[Path] = set()
    deadline = now() + timeout

    def emit(kind: str, msg: str) -> None:
        if on_event:
            on_event(kind, msg)

    while pending and now() < deadline:
        for partial in _partials(downloads_dir, since):
            emit("waiting", f"still downloading: {partial.name}")
            break

        still_pending = []
        for item in pending:
            src = find_download(item, downloads_dir, since, claimed=claimed)
            if src is None:
                still_pending.append(item)
                continue
            # Give Chrome a beat to finish flushing, then confirm the size settled.
            first = src.stat().st_size
            time.sleep(0.25)
            if src.stat().st_size != first:
                still_pending.append(item)
                continue

            target = unique_path(Path(item["target"]))
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(target))
            claimed.add(src)
            moved.append({**item, "saved_to": str(target), "bytes": target.stat().st_size})
            emit("moved", f"{src.name} -> {target}")

        pending = still_pending
        if pending:
            time.sleep(poll)

    return {"moved": moved, "missing": pending}


def import_paths(sources: Iterable[str | Path], targets_dir: Path) -> list[dict]:
    """Move already-downloaded files into a ticket folder (manual escape hatch)."""
    out = []
    for raw in sources:
        src = Path(raw).expanduser()
        if not src.is_file():
            out.append({"source": str(src), "error": "not a file"})
            continue
        target = unique_path(targets_dir / src.name)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(target))
        out.append({"source": str(src), "saved_to": str(target), "bytes": target.stat().st_size})
    return out
