"""Thin ffmpeg/ffprobe layer: probing, timestamp parsing, audio extraction."""

from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path


class MediaError(RuntimeError):
    pass


@lru_cache(maxsize=8)
def _tool(name: str) -> str:
    found = shutil.which(name)
    if not found:
        raise MediaError(f"`{name}` was not found on PATH. Install it with:  brew install ffmpeg")
    return found


def ffmpeg() -> str:
    return _tool("ffmpeg")


def ffprobe() -> str:
    return _tool("ffprobe")


def run(cmd: list[str], *, timeout: int | None = None) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise MediaError("\n".join(tail[-8:]) or f"{cmd[0]} exited {proc.returncode}")
    return proc


def _fraction(value: str | None) -> float:
    if not value or "/" not in value:
        try:
            return float(value or 0)
        except ValueError:
            return 0.0
    num, _, den = value.partition("/")
    try:
        den_f = float(den)
        return float(num) / den_f if den_f else 0.0
    except ValueError:
        return 0.0


def probe(path: str | Path) -> dict:
    """Normalized media metadata. Raises MediaError if the file is unreadable."""
    path = Path(path)
    if not path.is_file():
        raise MediaError(f"no such file: {path}")
    proc = run([
        ffprobe(), "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ])
    data = json.loads(proc.stdout or "{}")
    streams = data.get("streams", [])
    fmt = data.get("format", {})
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    duration = 0.0
    for source in (fmt.get("duration"), (video or {}).get("duration"), (audio or {}).get("duration")):
        try:
            duration = float(source)
            break
        except (TypeError, ValueError):
            continue

    info = {
        "path": str(path),
        "filename": path.name,
        "size_bytes": int(fmt.get("size") or path.stat().st_size),
        "format": fmt.get("format_name", ""),
        "duration": round(duration, 3),
        "has_video": video is not None,
        "has_audio": audio is not None,
        "video_codec": (video or {}).get("codec_name"),
        "audio_codec": (audio or {}).get("codec_name"),
        "width": int((video or {}).get("width") or 0),
        "height": int((video or {}).get("height") or 0),
        "fps": round(_fraction((video or {}).get("avg_frame_rate")) or
                     _fraction((video or {}).get("r_frame_rate")), 3),
    }
    # Animated GIFs / stills report a video stream but are not really "video".
    if info["has_video"] and info["duration"] <= 0.05 and not info["has_audio"]:
        info["is_still"] = True
    else:
        info["is_still"] = False
    return info


_DUR_RE = re.compile(r"(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>ms|s|m|h)?", re.I)


def parse_ts(value: str | float | int) -> float:
    """Accept 90, '90', '90s', '1:30', '00:01:30.5', '00:01:30,500', '1m30s', '500ms'."""
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", ".")
    if not text:
        raise ValueError("empty timestamp")

    if ":" in text:
        parts = text.split(":")
        if len(parts) > 3:
            raise ValueError(f"bad timestamp: {value!r}")
        total = 0.0
        for part in parts:
            total = total * 60 + float(part or 0)
        return total

    try:
        return float(text)
    except ValueError:
        pass

    total, matched = 0.0, False
    for m in _DUR_RE.finditer(text):
        num = float(m.group("num"))
        unit = (m.group("unit") or "s").lower()
        total += num * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unit]
        matched = True
    if not matched:
        raise ValueError(f"bad timestamp: {value!r}")
    return total


def parse_range(value: str) -> tuple[float, float]:
    """'00:01:20-00:01:40' or '80..100' or '1:20 to 1:40'."""
    text = str(value).strip()
    for sep in (" to ", "..", "--", "—"):
        if sep in text:
            a, b = text.split(sep, 1)
            return parse_ts(a), parse_ts(b)
    # A bare '-' is ambiguous with negative numbers but not with our formats.
    m = re.match(r"^(.*?)\s*-\s*(.*)$", text)
    if m and m.group(1) and m.group(2):
        return parse_ts(m.group(1)), parse_ts(m.group(2))
    raise ValueError(f"bad range: {value!r} (expected START-END, e.g. 00:01:20-00:01:40)")


def fmt_ts(seconds: float, style: str = "clock") -> str:
    """clock -> 00:01:23.500 | srt -> 00:01:23,500 | file -> 00-01-23.500."""
    seconds = max(0.0, float(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    millis = int(round((secs - math.floor(secs)) * 1000))
    secs = int(math.floor(secs))
    if millis == 1000:  # rounding spill
        millis, secs = 0, secs + 1
        if secs == 60:
            secs, minutes = 0, minutes + 1
    base = f"{int(hours):02d}:{int(minutes):02d}:{secs:02d}"
    if style == "srt":
        return f"{base},{millis:03d}"
    if style == "file":
        return base.replace(":", "-") + f".{millis:03d}"
    return f"{base}.{millis:03d}"


def extract_audio(src: str | Path, dest: str | Path) -> Path:
    """16 kHz mono PCM -- what Whisper wants anyway."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    run([
        ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
        "-c:a", "pcm_s16le", str(dest),
    ])
    return dest
