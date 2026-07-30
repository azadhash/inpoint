"""Pull stills out of a video: single timestamps, ranges, or scene changes."""

from __future__ import annotations

import math
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .media import MediaError, ffmpeg, fmt_ts, parse_ts, probe, run

MAX_FRAMES = 400

_MAC_FONTS = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Menlo.ttc",
    "/System/Library/Fonts/Helvetica.ttc",
]


def _pil_font(size: int):
    """Homebrew's ffmpeg ships without libfreetype, so we burn labels in with Pillow."""
    from PIL import ImageFont

    for path in _MAC_FONTS:
        if Path(path).is_file():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def burn_label(path: Path, text: str) -> None:
    """Draw the timecode in the bottom-left corner of an already-written frame."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return  # labelling is a nicety, never a hard failure

    with Image.open(path) as opened:
        img = opened.convert("RGBA" if path.suffix.lower() == ".png" else "RGB")

    size = max(15, img.height // 24)
    font = _pil_font(size)
    draw = ImageDraw.Draw(img, "RGBA")
    x0, y0, x1, y1 = draw.textbbox((0, 0), text, font=font)
    tw, th = x1 - x0, y1 - y0
    pad = max(6, size // 3)

    box_x, box_y = pad, img.height - th - pad * 3
    draw.rectangle(
        [box_x, box_y, box_x + tw + pad * 2, box_y + th + pad * 2],
        fill=(0, 0, 0, 150),
    )
    draw.text((box_x + pad - x0, box_y + pad - y0), text, font=font, fill=(255, 255, 255, 255))

    if path.suffix.lower() in {".jpg", ".jpeg"}:
        img.save(path, quality=92, subsampling=0)
    elif path.suffix.lower() == ".webp":
        img.save(path, quality=92)
    else:
        img.save(path)


def timestamps_for_range(
    start: float,
    end: float,
    *,
    every: float | None = None,
    count: int | None = None,
) -> list[float]:
    """Evenly spaced sample points inside [start, end]."""
    if end < start:
        start, end = end, start
    span = end - start

    if count is not None:
        if count < 1:
            raise ValueError("--count must be >= 1")
        if count == 1:
            return [start + span / 2]
        step = span / (count - 1)
        return [round(start + i * step, 3) for i in range(count)]

    step = every if every else (span / 9 if span > 0 else 1.0)
    if step <= 0:
        raise ValueError("--every must be > 0")
    n = int(math.floor(span / step)) + 1
    if n > MAX_FRAMES:
        raise ValueError(
            f"that would be {n} frames (limit {MAX_FRAMES}). "
            f"Use a larger --every or a --count instead."
        )
    return [round(start + i * step, 3) for i in range(n)]


_SHOWINFO_RE = re.compile(r"pts_time:(\d+(?:\.\d+)?)")


def scene_timestamps(
    video: str | Path,
    *,
    threshold: float = 0.30,
    start: float | None = None,
    end: float | None = None,
) -> list[float]:
    """Timestamps where the picture changes by more than `threshold` (0-1)."""
    cmd = [ffmpeg(), "-hide_banner", "-nostats"]
    if start:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(video)]
    if end is not None:
        cmd += ["-to", f"{max(0.0, end - (start or 0)):.3f}"]
    cmd += ["-vf", f"select='gt(scene,{threshold})',showinfo", "-vsync", "vfr", "-f", "null", "-"]

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 and not proc.stderr:
        raise MediaError(f"scene detection failed for {video}")
    offset = start or 0.0
    seen = [round(float(m) + offset, 3) for m in _SHOWINFO_RE.findall(proc.stderr)]
    # showinfo reports the first frame too when it passes the filter; dedupe near-neighbours.
    out: list[float] = []
    for t in seen:
        if not out or t - out[-1] > 0.4:
            out.append(t)
    return out[:MAX_FRAMES]


def extract(
    video: str | Path,
    timestamps: list[float],
    out_dir: str | Path,
    *,
    fmt: str = "jpg",
    width: int | None = None,
    quality: int = 2,
    label: bool = False,
    prefix: str = "frame",
    overwrite: bool = True,
) -> list[dict]:
    """Write one still per timestamp. Returns a manifest, newest-first ordering preserved."""
    video = Path(video)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    fmt = fmt.lower().lstrip(".")
    if fmt not in {"jpg", "jpeg", "png", "webp"}:
        raise ValueError(f"unsupported frame format: {fmt}")
    if len(timestamps) > MAX_FRAMES:
        raise ValueError(f"{len(timestamps)} frames requested, limit is {MAX_FRAMES}")

    info = probe(video)
    if not info["has_video"]:
        raise MediaError(f"{video.name} has no video stream to grab frames from")
    duration = info["duration"] or 0.0

    results: list[dict] = []

    for ts in timestamps:
        ts = max(0.0, float(ts))
        if duration and ts > duration:
            raise ValueError(
                f"{fmt_ts(ts)} is past the end of {video.name} ({fmt_ts(duration)})"
            )
        clock = fmt_ts(ts)
        dest = out_dir / f"{prefix}_{fmt_ts(ts, 'file')}.{fmt}"
        if dest.exists() and not overwrite:
            results.append(_entry(ts, clock, dest))
            continue

        chain = []
        if width:
            chain.append(f"scale={int(width)}:-2:flags=lanczos")

        cmd = [
            ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{ts:.3f}", "-i", str(video), "-frames:v", "1",
        ]
        if chain:
            cmd += ["-vf", ",".join(chain)]
        if fmt in {"jpg", "jpeg"}:
            cmd += ["-q:v", str(quality)]
        elif fmt == "webp":
            cmd += ["-quality", str(max(1, 100 - quality * 8))]
        cmd.append(str(dest))

        run(cmd)
        if not dest.is_file() or dest.stat().st_size == 0:
            raise MediaError(f"ffmpeg produced no frame at {clock} for {video.name}")
        if label:
            burn_label(dest, clock)
        results.append(_entry(ts, clock, dest))

    return results


def _entry(ts: float, clock: str, dest: Path) -> dict:
    return {
        "time": round(ts, 3),
        "label": clock,
        "name": dest.name,
        "path": str(dest),
        "size_bytes": dest.stat().st_size if dest.is_file() else 0,
    }


def contact_sheet(
    frames: list[dict],
    dest: str | Path,
    *,
    cols: int | None = None,
    tile_width: int = 480,
) -> Path:
    """Tile the given frames into one overview image."""
    if not frames:
        raise ValueError("no frames to build a contact sheet from")
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    n = len(frames)
    cols = cols or min(n, max(1, int(math.ceil(math.sqrt(n)))))
    rows = int(math.ceil(n / cols))

    with tempfile.TemporaryDirectory(prefix="asst-sheet-") as tmp:
        tmp_dir = Path(tmp)
        ext = Path(frames[0]["path"]).suffix or ".jpg"
        for i, frame in enumerate(frames, start=1):
            shutil.copy2(frame["path"], tmp_dir / f"{i:05d}{ext}")
        run([
            ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
            "-framerate", "1", "-i", str(tmp_dir / f"%05d{ext}"),
            "-filter_complex",
            f"scale={tile_width}:-2:flags=lanczos,tile={cols}x{rows}:padding=8:margin=8:color=black",
            "-frames:v", "1", "-q:v", "3", str(dest),
        ])
    return dest


def resolve_timestamps(
    video: str | Path,
    *,
    at: list[str] | None = None,
    time_range: str | None = None,
    every: str | None = None,
    count: int | None = None,
    scenes: bool = False,
    threshold: float = 0.30,
) -> list[float]:
    """Turn CLI-ish arguments into a concrete list of seconds."""
    start = end = None
    if time_range:
        from .media import parse_range
        start, end = parse_range(time_range)

    if scenes:
        found = scene_timestamps(video, threshold=threshold, start=start, end=end)
        if not found:
            raise MediaError(
                f"no scene changes above {threshold} were found -- try a lower --threshold"
            )
        return found

    if at:
        return sorted({round(parse_ts(a), 3) for a in at})

    if time_range:
        return timestamps_for_range(
            start, end,
            every=parse_ts(every) if every else None,
            count=count,
        )

    if count:
        duration = probe(video)["duration"]
        return timestamps_for_range(0.0, max(0.0, duration - 0.05), count=count)

    raise ValueError("give me one of --at, --range, --scenes, or --count")
