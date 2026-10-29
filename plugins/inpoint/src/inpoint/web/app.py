"""FastAPI app behind `inpoint serve`.

Drop a video in, get a transcript, pick a time range, get the frames. Everything
runs locally; the only state is a small registry mapping ids to file paths.
"""

from __future__ import annotations

import hashlib
import io
import json
import mimetypes
import re
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import frames as fr
from .. import jira, media, transcribe
from ..config import get_config
from ..paths import AUDIO_EXT, VIDEO_EXT, human_size, safe_name, sidecar_dir, slugify, unique_path

STATIC_DIR = Path(__file__).resolve().parent / "static"


# ---------------------------------------------------------------- registry

class Registry:
    """Persistent id -> path map so links survive a restart."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._data: dict[str, str] = {}
        if path.is_file():
            try:
                self._data = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                self._data = {}

    @staticmethod
    def make_id(path: Path) -> str:
        return hashlib.sha1(str(path.resolve()).encode()).hexdigest()[:12]

    def add(self, path: Path) -> str:
        path = path.resolve()
        mid = self.make_id(path)
        with self._lock:
            if self._data.get(mid) != str(path):
                self._data[mid] = str(path)
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps(self._data, indent=2))
        return mid

    def get(self, mid: str) -> Path:
        raw = self._data.get(mid)
        if not raw:
            raise HTTPException(404, f"unknown media id {mid}")
        path = Path(raw)
        if not path.is_file():
            raise HTTPException(410, f"file has moved or been deleted: {path}")
        return path


# ---------------------------------------------------------------- jobs

class Jobs:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, dict] = {}
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="inpoint-job")

    def create(self, kind: str, media_id: str) -> dict:
        job = {
            "id": uuid.uuid4().hex[:12], "kind": kind, "media_id": media_id,
            "status": "queued", "stage": "queued", "progress": 0.0, "message": "",
            "result": None, "error": None, "log": [],
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "started": time.time(),
        }
        with self._lock:
            self._jobs[job["id"]] = job
        return job

    def get(self, jid: str) -> dict:
        job = self._jobs.get(jid)
        if not job:
            raise HTTPException(404, f"unknown job {jid}")
        return job

    def update(self, jid: str, **fields) -> None:
        with self._lock:
            self._jobs[jid].update(fields)

    def append_log(self, jid: str, line: str) -> None:
        with self._lock:
            log = self._jobs[jid]["log"]
            if line and (not log or log[-1] != line):
                log.append(line)
                del log[:-40]

    def submit(self, job: dict, fn) -> None:
        def runner() -> None:
            self.update(job["id"], status="running", started=time.time())
            try:
                result = fn(job)
                self.update(job["id"], status="done", progress=1.0, stage="done",
                            result=result, elapsed=round(time.time() - job["started"], 1))
            except Exception as exc:  # surfaced verbatim in the UI
                self.update(job["id"], status="error", error=f"{type(exc).__name__}: {exc}",
                            elapsed=round(time.time() - job["started"], 1))
        self._pool.submit(runner)


# ---------------------------------------------------------------- payloads

class RegisterBody(BaseModel):
    path: str


class TranscribeBody(BaseModel):
    force: bool = False
    translate: bool = True
    language: str | None = None
    model: str | None = None


class FramesBody(BaseModel):
    mode: str = "range"                # range | at | scenes
    start: float | None = None
    end: float | None = None
    every: float | None = None
    count: int | None = None
    at: list[float] | None = None
    threshold: float = 0.30
    width: int | None = 1280
    fmt: str = "jpg"
    label: bool = False


# ---------------------------------------------------------------- helpers

def media_payload(path: Path, mid: str) -> dict:
    info = media.probe(path)
    t_dir = sidecar_dir(path, "transcripts")
    f_dir = sidecar_dir(path, "frames")
    transcript = transcribe.load_transcript(t_dir)
    frame_files = sorted(f_dir.glob("frame_*")) if f_dir.is_dir() else []
    return {
        "id": mid,
        "path": str(path),
        "name": path.name,
        "folder": str(path.parent),
        "ticket": _ticket_of(path),
        "duration": info["duration"],
        "duration_label": media.fmt_ts(info["duration"]),
        "width": info["width"], "height": info["height"],
        "size_bytes": info["size_bytes"], "size_label": human_size(info["size_bytes"]),
        "has_audio": info["has_audio"], "has_video": info["has_video"],
        "transcript_dir": str(t_dir),
        "frames_dir": str(f_dir),
        "has_transcript": transcript is not None,
        "language": (transcript or {}).get("language"),
        "has_translation": bool((transcript or {}).get("translation")),
        "frame_count": len(frame_files),
    }


_FRAME_RE = re.compile(r"^(?P<prefix>.+)_(?P<h>\d{2})-(?P<m>\d{2})-(?P<s>\d{2})\.(?P<ms>\d{3})\.\w+$")


def existing_frames(path: Path, mid: str) -> list[dict]:
    """Frames already on disk for this media, newest extraction included.

    Names encode their own timestamp, so nothing extra needs storing. Anything
    that doesn't match the pattern (contact sheets, strays) is ignored.
    """
    out_dir = sidecar_dir(path, "frames")
    if not out_dir.is_dir():
        return []
    found: list[dict] = []
    for item in out_dir.iterdir():
        if not item.is_file() or item.name.startswith("."):
            continue
        m = _FRAME_RE.match(item.name)
        if not m:
            continue
        seconds = (int(m["h"]) * 3600 + int(m["m"]) * 60 + int(m["s"])
                   + int(m["ms"]) / 1000)
        found.append({
            "time": round(seconds, 3),
            "label": media.fmt_ts(seconds),
            "name": item.name,
            "path": str(item),
            "size_bytes": item.stat().st_size,
            "url": f"/api/media/{mid}/frame/{item.name}",
        })
    found.sort(key=lambda f: (f["time"], f["name"]))
    return found


def annotate_said(path: Path, items: list[dict]) -> list[dict]:
    data = transcribe.load_transcript(sidecar_dir(path, "transcripts"))
    for frame in items:
        frame["said"] = None
        if data:
            hits = transcribe.segments_in_range(data, frame["time"] - 0.01, frame["time"] + 0.01)
            frame["said"] = " ".join(h["text"] for h in hits) or None
    return items


def _ticket_of(path: Path) -> dict | None:
    for parent in list(path.parents)[:3]:
        marker = parent / "ticket.json"
        if marker.is_file():
            try:
                data = json.loads(marker.read_text())
            except (OSError, json.JSONDecodeError):
                return None
            return {"key": data.get("key"), "summary": data.get("summary"),
                    "url": data.get("url"), "folder": str(parent)}
    return None


def range_response(path: Path, request: Request) -> Response:
    """FileResponse with byte-range support so <video> can scrub."""
    size = path.stat().st_size
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    header = request.headers.get("range")
    if not header:
        return FileResponse(path, media_type=mime, headers={"Accept-Ranges": "bytes"})

    m = re.match(r"bytes=(\d*)-(\d*)", header.strip())
    if not m:
        raise HTTPException(416, "malformed Range header")
    raw_start, raw_end = m.group(1), m.group(2)
    if raw_start:
        start = int(raw_start)
        end = int(raw_end) if raw_end else size - 1
    else:
        length = int(raw_end or 0)
        start, end = max(0, size - length), size - 1
    if start >= size or start > end:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
    end = min(end, size - 1)

    def stream():
        remaining = end - start + 1
        with path.open("rb") as fh:
            fh.seek(start)
            while remaining > 0:
                chunk = fh.read(min(1024 * 512, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(stream(), status_code=206, media_type=mime, headers={
        "Content-Range": f"bytes {start}-{end}/{size}",
        "Content-Length": str(end - start + 1),
        "Accept-Ranges": "bytes",
    })


# ---------------------------------------------------------------- app

def create_app() -> FastAPI:
    cfg = get_config()
    uploads_dir = cfg.work_dir / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    registry = Registry(cfg.work_dir / "registry.json")
    jobs = Jobs()

    app = FastAPI(title="inpoint", docs_url=None, redoc_url=None)
    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        page = STATIC_DIR / "index.html"
        if not page.is_file():
            return HTMLResponse("<h1>inpoint</h1><p>static/index.html is missing.</p>", 500)
        return HTMLResponse(page.read_text())

    @app.get("/api/config")
    def api_config() -> dict:
        return {"jira_base_url": cfg.jira_base_url, "asset_root": str(cfg.asset_root),
                "work_dir": str(cfg.work_dir), "model": cfg.whisper_model}

    # ---- library

    @app.get("/api/library")
    def api_library() -> dict:
        groups: list[dict] = []

        tickets: list[dict] = []
        if cfg.asset_root.is_dir():
            for marker in sorted(cfg.asset_root.glob("*/*/ticket.json")):
                root = marker.parent
                files = jira.media_files(root)
                if not files:
                    continue
                try:
                    meta = json.loads(marker.read_text())
                except (OSError, json.JSONDecodeError):
                    continue
                tickets.append({
                    "label": f"{meta.get('key', root.name)} — {meta.get('summary', '')}".strip(" —"),
                    "folder": str(root),
                    "items": [_safe_payload(p, registry) for p in files],
                })
        if tickets:
            groups.append({"name": "Jira tickets", "sections": tickets})

        uploaded = sorted(
            (p for p in uploads_dir.rglob("*")
             if p.is_file() and p.suffix.lower() in VIDEO_EXT | AUDIO_EXT),
            key=lambda p: p.stat().st_mtime, reverse=True,
        )[:50]
        if uploaded:
            groups.append({"name": "Uploads", "sections": [
                {"label": "Dropped in here", "folder": str(uploads_dir),
                 "items": [_safe_payload(p, registry) for p in uploaded]}]})

        return {"groups": groups}

    def _safe_payload(path: Path, reg: Registry) -> dict:
        mid = reg.add(path)
        try:
            return media_payload(path, mid)
        except media.MediaError as exc:
            return {"id": mid, "path": str(path), "name": path.name, "error": str(exc)}

    @app.post("/api/upload")
    async def api_upload(file: UploadFile = File(...)) -> dict:
        name = safe_name(file.filename or "upload.mp4")
        folder = uploads_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{slugify(Path(name).stem)}"
        folder.mkdir(parents=True, exist_ok=True)
        dest = unique_path(folder / name)
        with dest.open("wb") as fh:
            while chunk := await file.read(1024 * 1024):
                fh.write(chunk)
        if dest.stat().st_size == 0:
            dest.unlink(missing_ok=True)
            raise HTTPException(400, "uploaded file was empty")
        return media_payload(dest, registry.add(dest))

    @app.post("/api/media")
    def api_register(body: RegisterBody) -> dict:
        path = Path(body.path).expanduser()
        if not path.is_file():
            raise HTTPException(404, f"no such file: {path}")
        return media_payload(path, registry.add(path))

    @app.get("/api/media/{mid}")
    def api_media(mid: str) -> dict:
        return media_payload(registry.get(mid), mid)

    @app.get("/api/media/{mid}/stream")
    def api_stream(mid: str, request: Request) -> Response:
        return range_response(registry.get(mid), request)

    # ---- transcription

    @app.post("/api/media/{mid}/transcribe")
    def api_transcribe(mid: str, body: TranscribeBody) -> dict:
        path = registry.get(mid)
        out_dir = sidecar_dir(path, "transcripts")
        existing = transcribe.load_transcript(out_dir)
        if existing and not body.force:
            job = jobs.create("transcribe", mid)
            jobs.update(job["id"], status="done", progress=1.0, stage="cached",
                        result={"cached": True, "media_id": mid})
            return job

        job = jobs.create("transcribe", mid)

        def work(j: dict) -> dict:
            def progress(stage: str, frac: float, msg: str) -> None:
                jobs.update(j["id"], stage=stage, progress=round(frac, 4), message=msg)
                if msg:
                    jobs.append_log(j["id"], msg)

            transcribe.transcribe_media(
                path, out_dir, model=body.model or cfg.whisper_model,
                translate=body.translate,
                translate_fallback=cfg.whisper_translate_model,
                language=body.language, progress=progress,
            )
            return {"cached": False, "media_id": mid}

        jobs.submit(job, work)
        return job

    @app.get("/api/jobs/{jid}")
    def api_job(jid: str) -> dict:
        return jobs.get(jid)

    @app.get("/api/media/{mid}/transcript")
    def api_transcript(mid: str, translated: bool = False) -> dict:
        path = registry.get(mid)
        data = transcribe.load_transcript(sidecar_dir(path, "transcripts"))
        if not data:
            raise HTTPException(404, "no transcript yet")
        source = data.get("translation") if translated else data
        if translated and not source:
            raise HTTPException(404, "no translation for this media")
        return {
            "media_id": mid, "language": source.get("language", data["language"]),
            "duration": data["media"]["duration"],
            "has_translation": bool(data.get("translation")),
            "text": source["text"], "segments": source["segments"],
            "dir": data and str(sidecar_dir(path, "transcripts")),
        }

    @app.get("/api/media/{mid}/subtitles.vtt")
    def api_vtt(mid: str, translated: bool = False) -> Response:
        path = registry.get(mid)
        data = transcribe.load_transcript(sidecar_dir(path, "transcripts"))
        if not data:
            raise HTTPException(404, "no transcript yet")
        source = data.get("translation") if translated else data
        if not source:
            raise HTTPException(404, "no translation for this media")
        return Response(transcribe.to_vtt(source["segments"]), media_type="text/vtt")

    # ---- frames

    @app.post("/api/media/{mid}/frames")
    def api_frames(mid: str, body: FramesBody) -> dict:
        path = registry.get(mid)
        out_dir = sidecar_dir(path, "frames")
        try:
            if body.mode == "at":
                stamps = sorted({round(float(t), 3) for t in (body.at or [])})
                if not stamps:
                    raise ValueError("no timestamps given")
            elif body.mode == "scenes":
                stamps = fr.scene_timestamps(path, threshold=body.threshold,
                                             start=body.start, end=body.end)
                if not stamps:
                    raise ValueError(f"no scene changes above {body.threshold} in that window")
            else:
                duration = media.probe(path)["duration"]
                start = max(0.0, body.start if body.start is not None else 0.0)
                end = min(duration, body.end if body.end is not None else duration)
                if end <= start:
                    raise ValueError("end must be after start")
                stamps = fr.timestamps_for_range(start, end, every=body.every, count=body.count)
            made = fr.extract(path, stamps, out_dir, fmt=body.fmt, width=body.width,
                              label=body.label)
        except (ValueError, media.MediaError) as exc:
            raise HTTPException(400, str(exc)) from exc

        for frame in made:
            frame["url"] = f"/api/media/{mid}/frame/{frame['name']}"
        annotate_said(path, made)
        return {"media_id": mid, "out_dir": str(out_dir), "count": len(made), "frames": made}

    @app.get("/api/media/{mid}/frames")
    def api_existing_frames(mid: str) -> dict:
        path = registry.get(mid)
        found = annotate_said(path, existing_frames(path, mid))
        return {"media_id": mid, "out_dir": str(sidecar_dir(path, "frames")),
                "count": len(found), "frames": found}

    @app.get("/api/media/{mid}/frame/{name}")
    def api_frame(mid: str, name: str) -> Response:
        path = registry.get(mid)
        target = (sidecar_dir(path, "frames") / name).resolve()
        if sidecar_dir(path, "frames").resolve() not in target.parents or not target.is_file():
            raise HTTPException(404, "no such frame")
        return FileResponse(target, media_type=mimetypes.guess_type(name)[0] or "image/jpeg")

    @app.get("/api/media/{mid}/frames.zip")
    def api_frames_zip(mid: str) -> Response:
        path = registry.get(mid)
        out_dir = sidecar_dir(path, "frames")
        items = sorted(out_dir.glob("*")) if out_dir.is_dir() else []
        items = [p for p in items if p.is_file()]
        if not items:
            raise HTTPException(404, "no frames extracted yet")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for item in items:
                zf.write(item, arcname=f"{path.stem}-frames/{item.name}")
        buf.seek(0)
        return StreamingResponse(buf, media_type="application/zip", headers={
            "Content-Disposition": f'attachment; filename="{slugify(path.stem)}-frames.zip"'})

    @app.exception_handler(media.MediaError)
    def _media_error(_request: Request, exc: media.MediaError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    return app
