"""Whisper transcription with word-level timestamps, plus an English translation pass.

Runs locally on the Apple-Silicon GPU through mlx-whisper; falls back to
openai-whisper elsewhere. Nothing leaves the machine.
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import media
from .media import MediaError, fmt_ts

Progress = Callable[[str, float, str], None]  # stage, 0..1, message


class TranscribeError(RuntimeError):
    pass


def cannot_translate(model: str) -> bool:
    """Whisper's turbo weights were distilled for transcription only.

    Asking them for task="translate" doesn't error -- it just returns the source
    language, which looks like a working translation until you read it.
    """
    return "turbo" in (model or "").lower()


# ---------------------------------------------------------------- backend

def _load_backend():
    """Pick a Whisper implementation.

    MLX (Apple Silicon GPU) is preferred when available, openai-whisper (CPU) is
    the portable fallback. Set ASST_WHISPER_BACKEND=mlx|openai to force one.
    """
    import os

    preferred = os.getenv("ASST_WHISPER_BACKEND", "").strip().lower()

    if preferred in {"openai", "cpu", "whisper"}:
        try:
            import whisper  # type: ignore
            return "openai", whisper
        except ImportError as exc:
            raise TranscribeError(
                "ASST_WHISPER_BACKEND asked for openai-whisper, but it isn't installed. "
                "Install it with:  uv pip install openai-whisper"
            ) from exc

    if preferred in {"mlx", "gpu"}:
        try:
            import mlx_whisper  # type: ignore
            return "mlx", mlx_whisper
        except ImportError as exc:
            raise TranscribeError(
                "ASST_WHISPER_BACKEND asked for mlx, but mlx-whisper isn't installed. "
                "It only supports Apple Silicon."
            ) from exc

    try:
        import mlx_whisper  # type: ignore
        return "mlx", mlx_whisper
    except ImportError:
        pass
    try:
        import whisper  # type: ignore
        return "openai", whisper
    except ImportError as exc:
        raise TranscribeError(
            "No Whisper backend installed. Run setup.sh, or install one directly:\n"
            "  Apple Silicon:  uv pip install mlx-whisper\n"
            "  everywhere else: uv pip install openai-whisper"
        ) from exc


_LINE_RE = re.compile(
    r"^\[(?P<start>[\d:.]+)\s*-->\s*(?P<end>[\d:.]+)\]\s?(?P<text>.*)$"
)


def _parse_clock(text: str) -> float:
    parts = text.split(":")
    total = 0.0
    for part in parts:
        total = total * 60 + float(part or 0)
    return total


class _StreamTap(io.TextIOBase):
    """Captures Whisper's verbose stdout so we can report real progress."""

    def __init__(self, on_segment: Callable[[float, str], None]):
        self._buf = ""
        self._on_segment = on_segment

    def write(self, chunk: str) -> int:  # type: ignore[override]
        self._buf += chunk
        while "\n" in self._buf:
            line, _, self._buf = self._buf.partition("\n")
            m = _LINE_RE.match(line.strip())
            if m:
                try:
                    self._on_segment(_parse_clock(m.group("end")), m.group("text").strip())
                except ValueError:
                    pass
        return len(chunk)

    def flush(self) -> None:  # type: ignore[override]
        return None


def _run_whisper(
    backend: tuple[str, object],
    audio: Path,
    *,
    model: str,
    task: str,
    language: str | None,
    duration: float,
    progress: Progress | None,
    stage: str,
    stage_lo: float,
    stage_hi: float,
) -> dict:
    kind, mod = backend

    def on_segment(end: float, text: str) -> None:
        if progress and duration > 0:
            frac = min(1.0, end / duration)
            progress(stage, stage_lo + (stage_hi - stage_lo) * frac, text[:160])

    tap = _StreamTap(on_segment)
    with contextlib.redirect_stdout(tap):
        if kind == "mlx":
            result = mod.transcribe(  # type: ignore[attr-defined]
                str(audio),
                path_or_hf_repo=model,
                task=task,
                language=language,
                word_timestamps=True,
                verbose=True,
                condition_on_previous_text=False,
            )
        else:
            # "mlx-community/whisper-large-v3-mlx" -> "large-v3", which is what
            # openai-whisper calls the same weights.
            name = model.split("/")[-1]
            for prefix in ("whisper-",):
                if name.startswith(prefix):
                    name = name[len(prefix):]
            if name.endswith("-mlx"):
                name = name[: -len("-mlx")]
            loaded = mod.load_model(name)  # type: ignore[attr-defined]
            result = loaded.transcribe(
                str(audio), task=task, language=language,
                word_timestamps=True, verbose=True,
            )
    return result


# ---------------------------------------------------------------- formatting

def _clean_segments(raw: list[dict]) -> list[dict]:
    out = []
    for i, seg in enumerate(raw or []):
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        words = [
            {
                "word": (w.get("word") or "").strip(),
                "start": round(float(w.get("start", 0)), 3),
                "end": round(float(w.get("end", 0)), 3),
            }
            for w in (seg.get("words") or [])
            if (w.get("word") or "").strip()
        ]
        out.append({
            "id": i,
            "start": round(float(seg.get("start", 0)), 3),
            "end": round(float(seg.get("end", 0)), 3),
            "text": text,
            "words": words,
        })
    return out


def to_srt(segments: list[dict]) -> str:
    lines = []
    for i, seg in enumerate(segments, start=1):
        lines.append(str(i))
        lines.append(f"{fmt_ts(seg['start'], 'srt')} --> {fmt_ts(seg['end'], 'srt')}")
        lines.append(seg["text"])
        lines.append("")
    return "\n".join(lines)


def to_vtt(segments: list[dict]) -> str:
    lines = ["WEBVTT", ""]
    for seg in segments:
        lines.append(f"{fmt_ts(seg['start'])} --> {fmt_ts(seg['end'])}")
        lines.append(seg["text"])
        lines.append("")
    return "\n".join(lines)


def to_timed_text(segments: list[dict]) -> str:
    return "\n".join(f"[{fmt_ts(s['start'])}] {s['text']}" for s in segments)


def to_plain_text(segments: list[dict]) -> str:
    """Wrap into readable paragraphs, breaking on gaps longer than 2s."""
    paras, current, prev_end = [], [], None
    for seg in segments:
        if prev_end is not None and seg["start"] - prev_end > 2.0 and current:
            paras.append(" ".join(current))
            current = []
        current.append(seg["text"])
        prev_end = seg["end"]
    if current:
        paras.append(" ".join(current))
    return "\n\n".join(paras)


# ---------------------------------------------------------------- entrypoint

def transcript_dir_for(media_path: Path, out_dir: Path | None = None) -> Path:
    if out_dir:
        return Path(out_dir)
    return media_path.parent.parent / "transcripts" / media_path.stem


def load_transcript(directory: str | Path) -> dict | None:
    path = Path(directory) / "transcript.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def transcribe_media(
    path: str | Path,
    out_dir: str | Path,
    *,
    model: str,
    translate: bool = True,
    translate_model: str | None = None,
    translate_fallback: str | None = None,
    language: str | None = None,
    progress: Progress | None = None,
) -> dict:
    """Transcribe (and optionally translate) one media file. Writes a set of
    sidecar files into `out_dir` and returns the transcript dict."""
    src = Path(path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    def report(stage: str, frac: float, msg: str = "") -> None:
        if progress:
            progress(stage, max(0.0, min(1.0, frac)), msg)

    report("probing", 0.01, f"Reading {src.name}")
    info = media.probe(src)
    if not info["has_audio"]:
        raise TranscribeError(f"{src.name} has no audio track to transcribe")
    duration = info["duration"]

    started = time.time()
    backend = _load_backend()

    with tempfile.TemporaryDirectory(prefix="asst-audio-") as tmp:
        wav = Path(tmp) / "audio.wav"
        report("audio", 0.03, "Extracting 16 kHz mono audio")
        media.extract_audio(src, wav)

        report("transcribe", 0.05, f"Loading {model}")
        raw = _run_whisper(
            backend, wav, model=model, task="transcribe", language=language,
            duration=duration, progress=progress, stage="transcribe",
            stage_lo=0.05, stage_hi=0.70 if translate else 0.97,
        )
        segments = _clean_segments(raw.get("segments") or [])
        detected = (raw.get("language") or language or "unknown").lower()

        translation = None
        translation_note = None
        if translate and detected not in {"en", "english"} and segments:
            tr_model = translate_model or model
            if cannot_translate(tr_model):
                fallback = translate_fallback or ""
                if not fallback or cannot_translate(fallback):
                    translation_note = (
                        f"{tr_model} cannot translate (turbo weights are "
                        "transcription-only). Set WHISPER_TRANSLATE_MODEL to a "
                        "non-turbo model to enable translation."
                    )
                    tr_model = None
                else:
                    report("translate", 0.71,
                           f"{tr_model.split('/')[-1]} can't translate — using "
                           f"{fallback.split('/')[-1]} (downloads once)")
                    tr_model = fallback

            if tr_model:
                report("translate", 0.72, f"Translating {detected} -> English")
                try:
                    raw_en = _run_whisper(
                        backend, wav, model=tr_model, task="translate", language=language,
                        duration=duration, progress=progress, stage="translate",
                        stage_lo=0.72, stage_hi=0.97,
                    )
                except Exception as exc:  # a missing model shouldn't lose the transcript
                    translation_note = f"translation failed with {tr_model}: {exc}"
                else:
                    en_segments = _clean_segments(raw_en.get("segments") or [])
                    if en_segments:
                        translation = {
                            "language": "en",
                            "model": tr_model,
                            "text": to_plain_text(en_segments),
                            "segments": en_segments,
                        }
                    else:
                        translation_note = f"{tr_model} returned no translated segments"

    transcript = {
        "schema": 1,
        "media": {
            "path": str(src),
            "filename": src.name,
            "duration": duration,
            "width": info["width"],
            "height": info["height"],
            "size_bytes": info["size_bytes"],
            "has_video": info["has_video"],
        },
        "model": model,
        "backend": backend[0],
        "language": detected,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "elapsed_seconds": round(time.time() - started, 1),
        "text": to_plain_text(segments),
        "segments": segments,
        "translation": translation,
        "translation_note": translation_note,
    }

    report("writing", 0.98, "Writing transcript files")
    _write_outputs(out_dir, transcript)
    report("done", 1.0, f"{len(segments)} segments in {transcript['elapsed_seconds']}s")
    return transcript


def _write_outputs(out_dir: Path, transcript: dict) -> None:
    segs = transcript["segments"]
    (out_dir / "transcript.json").write_text(json.dumps(transcript, indent=2, ensure_ascii=False))
    (out_dir / "transcript.srt").write_text(to_srt(segs))
    (out_dir / "transcript.vtt").write_text(to_vtt(segs))
    (out_dir / "transcript.txt").write_text(to_plain_text(segs) + "\n")
    (out_dir / "transcript.timed.txt").write_text(to_timed_text(segs) + "\n")

    tr = transcript.get("translation")
    if tr:
        (out_dir / "transcript.en.srt").write_text(to_srt(tr["segments"]))
        (out_dir / "transcript.en.vtt").write_text(to_vtt(tr["segments"]))
        (out_dir / "transcript.en.txt").write_text(tr["text"] + "\n")
        (out_dir / "transcript.en.timed.txt").write_text(to_timed_text(tr["segments"]) + "\n")


# ---------------------------------------------------------------- queries

def segments_in_range(transcript: dict, start: float, end: float, *, translated: bool = False) -> list[dict]:
    source = transcript.get("translation") if translated else transcript
    segs = (source or {}).get("segments") or []
    return [s for s in segs if s["end"] > start and s["start"] < end]

def search(transcript: dict, query: str, *, translated: bool = False) -> list[dict]:
    source = transcript.get("translation") if translated else transcript
    segs = (source or {}).get("segments") or []
    needle = query.strip().lower()
    if not needle:
        return []
    return [s for s in segs if needle in s["text"].lower()]
