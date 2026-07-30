"""Folder layout for harvested tickets, plus attachment classification.

Layout:
    <ASSET_ROOT>/<PROJECT_KEY>/<TICKET_KEY>/
        ticket.json          full issue metadata + attachment manifest
        README.md            human-readable summary
        images/
        videos/
        files/
        transcripts/<video-stem>/transcript.{json,srt,vtt,txt}
        frames/<video-stem>/frame_00-01-23.500.jpg
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from .config import Config

IMAGE_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".heif",
    ".bmp", ".tif", ".tiff", ".svg", ".avif",
}
VIDEO_EXT = {
    ".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv",
    ".mpeg", ".mpg", ".wmv", ".flv", ".ogv",
}
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".oga", ".flac", ".caf", ".aiff", ".wma"}

TICKET_RE = re.compile(r"\b([A-Z][A-Z0-9_]{1,20})-(\d+)\b")


def kind_for(filename: str, mime: str = "") -> str:
    """Return one of image / video / audio / file."""
    ext = Path(filename).suffix.lower()
    if ext in IMAGE_EXT:
        return "image"
    if ext in VIDEO_EXT:
        return "video"
    if ext in AUDIO_EXT:
        return "audio"
    mime = (mime or "").lower()
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "audio"
    return "file"


SUBDIR_FOR_KIND = {"image": "images", "video": "videos", "audio": "audio", "file": "files"}


def safe_name(name: str, fallback: str = "untitled") -> str:
    """Filesystem-safe filename that still looks like the original."""
    name = unicodedata.normalize("NFC", name).strip()
    name = name.replace("/", "-").replace("\\", "-").replace(":", "-")
    name = re.sub(r'[\x00-\x1f<>:"|?*]', "", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        return fallback
    # Leave room for a " (1)" style suffix inside the 255-byte HFS+/APFS limit.
    stem, dot, ext = name.rpartition(".")
    if dot and len(ext) <= 8:
        return stem[:200].strip(" .") + "." + ext
    return name[:200].strip(" .")


def slugify(value: str, fallback: str = "item") -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    value = re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-").lower()
    return value[:80] or fallback


def project_dir(cfg: Config, project_key: str) -> Path:
    return cfg.asset_root / safe_name(project_key, "UNKNOWN")


def ticket_dir(cfg: Config, project_key: str, ticket_key: str) -> Path:
    return project_dir(cfg, project_key) / safe_name(ticket_key, "UNKNOWN-0")


def ensure_ticket_dirs(root: Path) -> dict[str, Path]:
    dirs = {
        "root": root,
        "images": root / "images",
        "videos": root / "videos",
        "audio": root / "audio",
        "files": root / "files",
        "transcripts": root / "transcripts",
        "frames": root / "frames",
    }
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    return dirs


def find_ticket_dir(cfg: Config, ticket_key: str) -> Path | None:
    """Locate an already-harvested ticket without knowing its project key."""
    ticket_key = ticket_key.upper()
    guess = ticket_dir(cfg, ticket_key.rsplit("-", 1)[0], ticket_key)
    if guess.is_dir():
        return guess
    if not cfg.asset_root.is_dir():
        return None
    for candidate in sorted(cfg.asset_root.glob(f"*/{ticket_key}")):
        if candidate.is_dir():
            return candidate
    return None


def sidecar_dir(media_path: str | Path, kind: str) -> Path:
    """Where transcripts/frames for a media file live.

    Inside a harvested ticket that's <ticket>/<kind>/<stem>/; anywhere else it's
    a `<stem>.<kind>/` folder next to the file.
    """
    media_path = Path(media_path).resolve()
    parent = media_path.parent
    if parent.name in {"videos", "audio", "images"} and (parent.parent / "ticket.json").is_file():
        return parent.parent / kind / media_path.stem
    return parent / f"{media_path.stem}.{kind}"


def unique_path(path: Path) -> Path:
    """Return `path`, or `name (2).ext` etc. if it is already taken."""
    if not path.exists():
        return path
    stem, ext = path.stem, path.suffix
    for n in range(2, 1000):
        candidate = path.with_name(f"{stem} ({n}){ext}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"could not find a free name near {path}")


def human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024 or unit == "TB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} TB"
