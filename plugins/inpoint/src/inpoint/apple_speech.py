"""Apple's on-device speech recognition as a transcription backend (macOS 26+).

The heavy lifting is a small Swift helper in apple/transcribe.swift, compiled
once into the cache directory. It needs no model download: the OS manages the
speech assets. English and a handful of other languages only; Whisper remains
the fallback for everything else and for translation.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable

Progress = Callable[[str, float, str], None]

SOURCE = Path(__file__).resolve().parents[2] / "apple" / "transcribe.swift"
# ponytail: static list mirroring SpeechTranscriber.supportedLocales on macOS 26.
# The helper exits 3 for anything it can't do, so a stale entry just falls back.
LANGUAGES = {"en", "de", "es", "fr", "it", "ja", "ko", "pt", "zh", "yue"}
_LINE_RE = re.compile(r"^\[(?P<start>[\d.]+) --> (?P<end>[\d.]+)\] (?P<text>.*)$")


class AppleSpeechError(RuntimeError):
    pass


def unavailable_reason() -> str | None:
    """None when the backend can run here, otherwise a one-line reason."""
    if platform.system() != "Darwin":
        return "macOS only"
    major = int((platform.mac_ver()[0] or "0").split(".")[0] or 0)
    if major < 26:
        return f"needs macOS 26 or later (this is {platform.mac_ver()[0]})"
    if not SOURCE.is_file():
        return f"helper source missing: {SOURCE}"
    if not _binary().exists() and not shutil.which("swiftc"):
        return "swiftc not found — install Xcode Command Line Tools: xcode-select --install"
    return None


def wanted(language: str | None) -> bool:
    """Should Apple handle this request? Honors INPOINT_WHISPER_BACKEND."""
    pref = os.getenv("INPOINT_WHISPER_BACKEND", "").strip().lower()
    if pref == "apple":
        return True
    if pref:  # mlx / openai / cpu / gpu: the user asked for Whisper
        return False
    if unavailable_reason():
        return False
    # ponytail: no language given is assumed English. Upgrade path: a language-ID pass.
    code = (language or "en").lower().split("-")[0].split("_")[0]
    return code in LANGUAGES


def looks_sparse(result: dict, duration: float) -> bool:
    """Non-English speech through an English recogniser yields few, garbled words.
    Treat a very low word rate on a non-trivial clip as 'wrong language'."""
    words = sum(len(s.get("words") or []) for s in result.get("segments") or [])
    return duration > 15 and words / (duration / 60) < 30


def _binary() -> Path:
    cache = Path(os.getenv("XDG_CACHE_HOME") or Path.home() / ".cache") / "inpoint"
    digest = hashlib.sha256(SOURCE.read_bytes()).hexdigest()[:12] if SOURCE.is_file() else "none"
    return cache / f"apple-transcribe-{digest}"


def helper() -> Path:
    reason = unavailable_reason()
    if reason:
        raise AppleSpeechError(reason)
    binary = _binary()
    if not binary.exists():
        binary.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            ["swiftc", "-O", "-parse-as-library", "-o", str(binary), str(SOURCE)],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise AppleSpeechError(f"could not compile the Apple speech helper:\n{proc.stderr.strip()}")
    return binary


def transcribe(
    audio: Path,
    *,
    language: str | None,
    duration: float,
    progress: Progress | None,
    stage: str,
    stage_lo: float,
    stage_hi: float,
) -> dict:
    """Returns a Whisper-shaped result: {"language", "segments": [{start, end, text, words}]}."""
    cmd = [str(helper()), str(audio)]
    if language:
        cmd.append(language)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert proc.stderr is not None and proc.stdout is not None
    err_lines: list[str] = []
    for line in proc.stderr:
        line = line.rstrip("\n")
        m = _LINE_RE.match(line)
        if m and progress and duration > 0:
            frac = min(1.0, float(m.group("end")) / duration)
            progress(stage, stage_lo + (stage_hi - stage_lo) * frac, m.group("text")[:160])
        else:
            err_lines.append(line)
    out = proc.stdout.read()
    code = proc.wait()
    if code == 3:
        raise AppleSpeechError(f"Apple speech does not support language {language!r}")
    if code != 0:
        raise AppleSpeechError("\n".join(err_lines).strip() or f"helper exited {code}")
    data = json.loads(out)
    locale = data.get("locale") or (language or "en")
    return {"language": locale.split("_")[0].lower(), "segments": data.get("segments") or []}
