"""inpoint -- command line entry point.

    inpoint jira open ABC-123          print the ticket URL + the JS Claude runs to read it
    inpoint jira ingest --manifest -   create the folders, print the download plan
    inpoint jira collect ABC-123       file whatever Chrome just downloaded
    inpoint transcribe <video|KEY>     Whisper transcript + English translation
    inpoint frames <video> --range ... stills at the timestamps you care about
    inpoint transcript <video> --range what is being said between two timestamps
    inpoint library                    everything harvested so far, across all tickets
    inpoint serve                      the drag-and-drop web UI
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import downloads as dl
from . import frames as fr
from . import jira, media, transcribe
from .config import PROJECT_ROOT, get_config
from .paths import human_size, sidecar_dir


# ---------------------------------------------------------------- output helpers

def emit(payload: dict, args, lines: list[str] | None = None) -> None:
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    elif lines:
        print("\n".join(lines))


def note(*parts: str) -> None:
    print(*parts, file=sys.stderr)


def read_json_arg(value: str | None) -> dict:
    """Read JSON from '-' (stdin), a file path, or an inline string."""
    if value in (None, "-"):
        raw = sys.stdin.read()
    else:
        path = Path(value).expanduser()
        raw = path.read_text() if path.is_file() else value
    raw = raw.strip()
    if not raw:
        raise SystemExit("no manifest JSON received on stdin")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"manifest is not valid JSON: {exc}") from exc


def split_list(values: list[str] | None) -> list[str]:
    out: list[str] = []
    for value in values or []:
        out += [v.strip() for v in str(value).split(",") if v.strip()]
    return out


def reveal(path: Path) -> None:
    if sys.platform == "darwin" and shutil.which("open"):
        subprocess.run(["open", "-R", str(path)], check=False)


# ---------------------------------------------------------------- jira

def cmd_jira_open(args) -> int:
    cfg = get_config()
    key, url = jira.parse_ref(args.ticket, cfg)
    snippet = jira.fetch_snippet(key)
    base = url.split("/browse/")[0]
    emit(
        {"key": key, "url": url, "api_url": jira.api_url(key, base), "fetch_snippet": snippet},
        args,
        [
            f"Ticket : {key}",
            f"Open   : {url}",
            "",
            "Run this in that tab (returns the attachment manifest as JSON):",
            "",
            snippet,
        ],
    )
    return 0


def cmd_jira_ingest(args) -> int:
    cfg = get_config()
    manifest = read_json_arg(args.manifest)
    result = jira.build_plan(manifest, cfg)
    ticket, plan, folder = result["ticket"], result["plan"], result["folder"]

    ticket["since_epoch"] = time.time()
    jira.save_ticket(folder, ticket)

    payload = {
        "key": ticket["key"],
        "summary": ticket["summary"],
        "folder": str(folder),
        "since_epoch": ticket["since_epoch"],
        "attachment_count": len(plan),
        "plan": plan,
        "download_snippet": jira.download_snippet(plan) if plan else "",
        "next": f"inpoint jira collect {ticket['key']}",
    }
    lines = [
        f"{ticket['key']} — {ticket['summary']}",
        f"Folder: {folder}",
        f"Attachments: {len(plan)}",
    ]
    for item in plan:
        lines.append(f"  [{item['kind']:5}] {item['filename']}  ({human_size(item['size'])})")
    if plan:
        lines += ["", "Now run the download snippet in the Jira tab, then:",
                  f"  inpoint jira collect {ticket['key']}"]
    emit(payload, args, lines)
    return 0


def cmd_jira_collect(args) -> int:
    cfg = get_config()
    root, ticket = jira.load_ticket(cfg, args.ticket)
    plan = [a for a in ticket.get("attachments", []) if a.get("url")]
    if args.only:
        wanted = set(split_list(args.only))
        plan = [a for a in plan if a["filename"] in wanted or str(a.get("id")) in wanted]
    pending = [a for a in plan if not (a.get("saved_to") and Path(a["saved_to"]).is_file())]

    if not pending:
        emit({"key": ticket["key"], "moved": [], "missing": [], "folder": str(root),
              "message": "everything already filed"}, args,
             [f"{ticket['key']}: all {len(plan)} attachments are already in {root}"])
        return 0

    since = args.since if args.since is not None else ticket.get("since_epoch", time.time() - 900)
    if not getattr(args, "json", False):
        note(f"Watching {cfg.downloads_dir} for {len(pending)} file(s), up to {args.timeout}s...")

    result = dl.collect(
        pending, cfg.downloads_dir, since=since, timeout=args.timeout,
        on_event=(None if getattr(args, "json", False)
                  else lambda kind, msg: note(f"  {kind}: {msg}")),
    )
    jira.mark_collected(root, ticket, result["moved"])

    payload = {
        "key": ticket["key"],
        "folder": str(root),
        "moved": result["moved"],
        "missing": [m["filename"] for m in result["missing"]],
    }
    lines = [f"Filed {len(result['moved'])} file(s) into {root}"]
    lines += [f"  {m['subdir']}/{Path(m['saved_to']).name}" for m in result["moved"]]
    if result["missing"]:
        lines += ["", f"Still missing ({len(result['missing'])}):"]
        lines += [f"  {m['filename']}" for m in result["missing"]]
        lines.append("Re-run the download snippet in the Jira tab, then collect again.")
    emit(payload, args, lines)
    if args.reveal:
        reveal(root)
    return 0


def cmd_jira_fetch(args) -> int:
    """Read the issue and download every attachment in one step, no browser."""
    from . import jira_api

    cfg = get_config()
    key, _ = jira.parse_ref(args.ticket, cfg)
    api = jira_api.JiraApi(cfg)

    if not getattr(args, "json", False):
        note(f"Fetching {key} from {cfg.jira_base_url} as {api.email}...")
    manifest = api.issue(key)
    result = jira.build_plan(manifest, cfg)
    ticket, plan, folder = result["ticket"], result["plan"], result["folder"]

    downloaded, failed = [], []
    for item in plan:
        target = Path(item["target"])
        if target.is_file() and target.stat().st_size == item["size"] and not args.force:
            item["saved_to"] = str(target)
            item["bytes"] = target.stat().st_size
            downloaded.append(item)
            if not getattr(args, "json", False):
                note(f"  have  {item['subdir']}/{target.name}")
            continue

        def progress(name: str, done: int, total: int) -> None:
            if getattr(args, "json", False) or not total:
                return
            pct = done / total
            bar = "#" * int(pct * 20) + "." * (20 - int(pct * 20))
            note(f"\r  [{bar}] {pct * 100:3.0f}%  {name[:52]:<52}")

        try:
            api.download(item["url"], target, on_progress=progress)
        except jira.JiraError as exc:
            failed.append({**item, "error": str(exc)})
            note(f"\n  FAILED {item['filename']}: {exc}")
            continue

        item["saved_to"] = str(target)
        item["bytes"] = target.stat().st_size
        downloaded.append(item)
        if not getattr(args, "json", False):
            note("")
            note(f"  saved {item['subdir']}/{target.name}  ({human_size(item['bytes'])})")

    jira.save_ticket(folder, ticket)
    jira.write_readme(folder, ticket)

    payload = {
        "key": ticket["key"], "summary": ticket["summary"], "folder": str(folder),
        "downloaded": [{"filename": d["filename"], "kind": d["kind"],
                        "saved_to": d["saved_to"], "bytes": d["bytes"]} for d in downloaded],
        "failed": [{"filename": f["filename"], "error": f["error"]} for f in failed],
    }
    lines = [f"{ticket['key']} — {ticket['summary']}", f"Folder: {folder}",
             f"Downloaded {len(downloaded)}/{len(plan)} attachment(s)"]
    if failed:
        lines += ["", "Failed:"] + [f"  {f['filename']}: {f['error']}" for f in failed]
    emit(payload, args, lines)
    if args.reveal:
        reveal(folder)
    return 1 if failed else 0


def cmd_jira_import(args) -> int:
    cfg = get_config()
    root, ticket = jira.load_ticket(cfg, args.ticket)
    from .paths import SUBDIR_FOR_KIND, kind_for
    moved = []
    for raw in args.files:
        src = Path(raw).expanduser()
        if not src.is_file():
            note(f"skip (not a file): {src}")
            continue
        sub = SUBDIR_FOR_KIND[kind_for(src.name)]
        moved += dl.import_paths([src], root / sub)
    emit({"key": ticket["key"], "folder": str(root), "moved": moved}, args,
         [f"Imported {len(moved)} file(s) into {root}"] +
         [f"  {m.get('saved_to', m.get('error'))}" for m in moved])
    return 0


def cmd_jira_status(args) -> int:
    cfg = get_config()
    root, ticket = jira.load_ticket(cfg, args.ticket)
    atts = ticket.get("attachments", [])
    have = [a for a in atts if a.get("saved_to") and Path(a["saved_to"]).is_file()]
    vids = jira.media_files(root)

    files = []
    for sub in ("images", "videos", "audio", "files"):
        d = root / sub
        if d.is_dir():
            for p in sorted(d.iterdir()):
                if p.is_file() and not p.name.startswith("."):
                    files.append({"kind": sub, "path": str(p), "size_bytes": p.stat().st_size})

    payload = {
        "key": ticket["key"], "summary": ticket["summary"], "url": ticket.get("url"),
        "folder": str(root), "downloaded": len(have), "total": len(atts),
        "files": files,
        "media": [{"path": str(v),
                   "transcript": str(sidecar_dir(v, "transcripts"))
                   if (sidecar_dir(v, "transcripts") / "transcript.json").is_file() else None}
                  for v in vids],
    }
    lines = [f"{ticket['key']} — {ticket['summary']}", f"Folder: {root}",
             f"Attachments filed: {len(have)}/{len(atts)}", ""]
    for f in files:
        lines.append(f"  {f['kind']:7} {Path(f['path']).name}  ({human_size(f['size_bytes'])})")
    if payload["media"]:
        lines += ["", "Media:"]
        for m in payload["media"]:
            state = "transcribed" if m["transcript"] else "not transcribed"
            lines.append(f"  {Path(m['path']).name}  [{state}]")
    emit(payload, args, lines)
    return 0


# ---------------------------------------------------------------- media commands

def resolve_media_targets(cfg, target: str) -> list[Path]:
    """A path, a directory, or a ticket key (-> every video/audio file in it)."""
    path = Path(target).expanduser()
    if path.is_file():
        return [path.resolve()]
    if path.is_dir():
        found = jira.media_files(path)
        if found:
            return found
        return sorted(p for p in path.iterdir() if p.is_file() and not p.name.startswith("."))
    root, _ = jira.load_ticket(cfg, target)
    found = jira.media_files(root)
    if not found:
        raise SystemExit(f"no video/audio files downloaded for {target} yet")
    return found


def cmd_transcribe(args) -> int:
    cfg = get_config()
    targets = resolve_media_targets(cfg, args.target)
    results = []

    for src in targets:
        out_dir = Path(args.out).expanduser() if args.out else sidecar_dir(src, "transcripts")
        existing = transcribe.load_transcript(out_dir)
        if existing and not args.force:
            note(f"{src.name}: already transcribed ({out_dir}) -- use --force to redo")
            results.append({"media": str(src), "dir": str(out_dir), "transcript": existing,
                            "reused": True})
            continue

        show = not getattr(args, "json", False)
        last = [0.0]

        def progress(stage: str, frac: float, msg: str) -> None:
            if not show or (frac - last[0] < 0.01 and stage not in {"done", "audio"}):
                return
            last[0] = frac
            width = 28
            done = int(frac * width)
            bar = "#" * done + "." * (width - done)
            note(f"\r  [{bar}] {frac * 100:3.0f}%  {stage:<10} {msg[:52]:<52}")

        if show:
            note(f"{src.name} ...")
        result = transcribe.transcribe_media(
            src, out_dir, model=args.model or cfg.whisper_model,
            translate=not args.no_translate,
            translate_model=args.translate_model,
            translate_fallback=cfg.whisper_translate_model,
            language=args.language,
            progress=progress if show else None,
        )
        if show:
            note("")
            note(f"  language: {result['language']}  segments: {len(result['segments'])}"
                 f"  took: {result['elapsed_seconds']}s")
            if result.get("translation_note"):
                note(f"  note    : {result['translation_note']}")
            note(f"  written : {out_dir}")
        results.append({"media": str(src), "dir": str(out_dir), "transcript": result,
                        "reused": False})

    payload = {"count": len(results), "results": [
        {"media": r["media"], "dir": r["dir"], "reused": r["reused"],
         "language": r["transcript"]["language"],
         "duration": r["transcript"]["media"]["duration"],
         "segments": len(r["transcript"]["segments"]),
         "translated": bool(r["transcript"].get("translation")),
         "translation_note": r["transcript"].get("translation_note"),
         "text": r["transcript"]["text"] if args.full_text else None,
         "files": sorted(p.name for p in Path(r["dir"]).glob("transcript.*"))}
        for r in results]}
    emit(payload, args, [])
    if not getattr(args, "json", False) and args.full_text:
        for r in results:
            print(f"\n===== {Path(r['media']).name} =====\n")
            print(transcribe.to_timed_text(r["transcript"]["segments"]))
    return 0


def cmd_transcript(args) -> int:
    cfg = get_config()
    src = resolve_media_targets(cfg, args.target)[0]
    out_dir = Path(args.out).expanduser() if args.out else sidecar_dir(src, "transcripts")
    data = transcribe.load_transcript(out_dir)
    if not data:
        raise SystemExit(f"no transcript at {out_dir} -- run:  inpoint transcribe {src}")

    segs = (data.get("translation") or data)["segments"] if args.translated else data["segments"]
    label = "en (translated)" if args.translated else data["language"]

    if args.range:
        start, end = media.parse_range(args.range)
        segs = [s for s in segs if s["end"] > start and s["start"] < end]
    if args.search:
        needle = args.search.lower()
        segs = [s for s in segs if needle in s["text"].lower()]

    payload = {"media": str(src), "language": label, "duration": data["media"]["duration"],
               "count": len(segs), "segments": segs,
               "text": " ".join(s["text"] for s in segs)}
    emit(payload, args, [f"{src.name}  [{label}]  {len(segs)} segment(s)", ""] +
         [f"[{media.fmt_ts(s['start'])} → {media.fmt_ts(s['end'])}] {s['text']}" for s in segs])
    return 0


def cmd_frames(args) -> int:
    cfg = get_config()
    src = resolve_media_targets(cfg, args.target)[0]
    out_dir = Path(args.out).expanduser() if args.out else sidecar_dir(src, "frames")

    timestamps = fr.resolve_timestamps(
        src, at=split_list(args.at), time_range=args.range, every=args.every,
        count=args.count, scenes=args.scenes, threshold=args.threshold,
    )
    made = fr.extract(
        src, timestamps, out_dir, fmt=args.format, width=args.width,
        quality=args.quality, label=args.label, prefix=args.prefix,
    )

    sheet = None
    if args.sheet and made:
        sheet = str(fr.contact_sheet(made, out_dir / "contact-sheet.jpg", cols=args.sheet_cols))

    # Pair each frame with whatever is being said at that moment, if we have a transcript.
    data = transcribe.load_transcript(sidecar_dir(src, "transcripts"))
    if data:
        for frame in made:
            hits = transcribe.segments_in_range(data, frame["time"] - 0.01, frame["time"] + 0.01)
            frame["said"] = " ".join(h["text"] for h in hits) or None

    payload = {"media": str(src), "out_dir": str(out_dir), "count": len(made),
               "frames": made, "contact_sheet": sheet}
    lines = [f"{len(made)} frame(s) from {src.name} → {out_dir}", ""]
    for f in made:
        extra = f"   \"{f['said'][:70]}\"" if f.get("said") else ""
        lines.append(f"  {f['label']}  {f['path']}{extra}")
    if sheet:
        lines += ["", f"  contact sheet: {sheet}"]
    emit(payload, args, lines)
    if args.reveal:
        reveal(Path(made[0]["path"]) if made else out_dir)
    return 0


def cmd_library(args) -> int:
    """Everything harvested so far -- the entry point when you don't know the key yet."""
    cfg = get_config()
    tickets: list[dict] = []

    if cfg.asset_root.is_dir():
        for marker in cfg.asset_root.glob("*/*/ticket.json"):
            root = marker.parent
            try:
                meta = json.loads(marker.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            project = (meta.get("project") or {}).get("key", "")
            if args.project and project.upper() != args.project.upper():
                continue

            counts = {}
            for sub in ("images", "videos", "audio", "files"):
                d = root / sub
                counts[sub] = len([p for p in d.iterdir()
                                   if p.is_file() and not p.name.startswith(".")]) if d.is_dir() else 0

            media = []
            for path in jira.media_files(root):
                t_dir = sidecar_dir(path, "transcripts")
                f_dir = sidecar_dir(path, "frames")
                data = transcribe.load_transcript(t_dir)
                media.append({
                    "name": path.name,
                    "path": str(path),
                    "transcript_dir": str(t_dir) if data else None,
                    "language": (data or {}).get("language"),
                    "translated": bool((data or {}).get("translation")),
                    "duration": (data or {}).get("media", {}).get("duration"),
                    "frames": len(list(f_dir.glob("frame_*"))) if f_dir.is_dir() else 0,
                    "frames_dir": str(f_dir) if f_dir.is_dir() else None,
                })

            tickets.append({
                "key": meta.get("key", root.name),
                "project": project,
                "summary": meta.get("summary", ""),
                "status": meta.get("status", ""),
                "url": meta.get("url", ""),
                "folder": str(root),
                "harvested_at": meta.get("harvested_at", ""),
                "counts": counts,
                "media": media,
                "mtime": root.stat().st_mtime,
            })

    tickets.sort(key=lambda t: t["mtime"], reverse=True)
    for t in tickets:
        t.pop("mtime", None)

    payload = {"asset_root": str(cfg.asset_root), "count": len(tickets), "tickets": tickets}
    lines = [f"{len(tickets)} ticket(s) under {cfg.asset_root}", ""]
    for t in tickets:
        lines.append(f"{t['key']}  {t['summary']}")
        lines.append(f"  {t['folder']}")
        have = [f"{n} {k}" for k, n in t["counts"].items() if n]
        lines.append(f"  {' · '.join(have) if have else 'nothing downloaded yet'}")
        for m in t["media"]:
            bits = []
            if m["transcript_dir"]:
                bits.append(f"transcribed ({m['language']}{'+en' if m['translated'] else ''})")
            else:
                bits.append("not transcribed")
            if m["frames"]:
                bits.append(f"{m['frames']} frames")
            lines.append(f"    {m['name']}  —  {' · '.join(bits)}")
        lines.append("")
    emit(payload, args, lines or ["nothing harvested yet"])
    return 0


def cmd_probe(args) -> int:
    info = media.probe(Path(args.target).expanduser())
    emit(info, args, [
        f"{info['filename']}",
        f"  duration : {media.fmt_ts(info['duration'])} ({info['duration']}s)",
        f"  size     : {human_size(info['size_bytes'])}",
        f"  video    : {info['video_codec'] or '—'} {info['width']}x{info['height']} @ {info['fps']}fps",
        f"  audio    : {info['audio_codec'] or '—'}",
    ])
    return 0


# ---------------------------------------------------------------- misc

def cmd_serve(args) -> int:
    import uvicorn
    from .web.app import create_app

    cfg = get_config()
    host = args.host or cfg.web_host
    port = args.port or cfg.web_port
    url = f"http://{host}:{port}"
    note(f"inpoint web UI  →  {url}")
    if args.open:
        subprocess.run(["open", url], check=False)
    uvicorn.run(create_app(), host=host, port=port, log_level="warning")
    return 0


TOKEN_URL = "https://id.atlassian.com/manage-profile/security/api-tokens"


def cmd_login(args) -> int:
    """Store a Jira API token so everything works without a browser."""
    from . import jira_api
    from .config import ENV_PATH, read_env_file, write_env

    cfg = get_config()
    existing = read_env_file()
    interactive = sys.stdin.isatty() and sys.stdout.isatty()

    site = args.site or existing.get("JIRA_BASE_URL") or cfg.jira_base_url
    email = args.email or existing.get("JIRA_EMAIL") or ""
    token = args.token or ""

    if interactive:
        print("Connect inpoint to Jira\n")
        print(f"  1. Open {TOKEN_URL}")
        print("  2. Create an API token and copy it")
        print("  3. Paste it below (it won't be echoed)\n")
        entered = input(f"Jira site URL [{site}]: ").strip()
        site = entered or site
        entered = input(f"Atlassian account email{f' [{email}]' if email else ''}: ").strip()
        email = entered or email
        if not token:
            import getpass
            token = getpass.getpass("API token: ").strip()
    elif not (email and token):
        note("Not running in a terminal, so I can't prompt. Pass the values instead:\n")
        note("  inpoint login --email you@company.com --token <TOKEN> [--site https://x.atlassian.net]\n")
        note(f"Create a token at {TOKEN_URL}")
        return 1

    site = (site or "").strip().rstrip("/")
    if site and not site.startswith("http"):
        site = f"https://{site}"
    if not (site and email and token):
        note("Need a site URL, an email, and a token.")
        return 1

    updated = dataclasses.replace(cfg, jira_base_url=site)

    note(f"\nChecking those credentials against {site} ...")
    try:
        api = jira_api.JiraApi(updated, email=email, token=token)
        me = api.whoami()
    except jira.JiraError as exc:
        note(f"\nRejected: {exc}")
        note("\nNothing was saved. Common causes: the token was copied incompletely, "
             "the email isn't the Atlassian account email, or the site URL is wrong.")
        return 1

    write_env({"JIRA_BASE_URL": site, "JIRA_EMAIL": email, "JIRA_API_TOKEN": token})

    display = me.get("displayName") or email
    payload = {"ok": True, "site": site, "email": email,
               "account": display, "env_file": str(ENV_PATH)}
    emit(payload, args, [
        f"\nConnected as {display} ({email})",
        f"Saved to {ENV_PATH} (owner-only permissions)",
        "",
        "You can now run everything headlessly:",
        "  inpoint jira fetch ABC-123",
    ])
    return 0


def cmd_logout(args) -> int:
    from .config import ENV_PATH, read_env_file, write_env

    if not read_env_file().get("JIRA_API_TOKEN"):
        emit({"ok": True, "message": "no token stored"}, args, ["No token was stored."])
        return 0
    write_env({"JIRA_API_TOKEN": ""})
    emit({"ok": True, "env_file": str(ENV_PATH)}, args,
         [f"Token cleared from {ENV_PATH}.",
          "The browser flow (inpoint jira open <KEY>) still works."])
    return 0


def cmd_doctor(args) -> int:
    cfg = get_config()
    checks = []

    def check(name: str, ok: bool, detail: str = "", required: bool = True) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail, "required": required})

    for tool in ("ffmpeg", "ffprobe"):
        path = shutil.which(tool)
        check(tool, bool(path), path or "missing — run: brew install ffmpeg")

    from . import apple_speech
    reason = apple_speech.unavailable_reason()
    check("apple speech", not reason,
          reason or "on-device, no download (English + 9 languages)", required=False)

    try:
        import mlx_whisper  # noqa: F401
        check("mlx-whisper", True, "Apple Silicon GPU backend ready")
    except ImportError:
        try:
            import whisper  # noqa: F401
            check("whisper", True, "openai-whisper (CPU) backend ready")
        except ImportError:
            check("whisper", False, "no backend — run ./setup.sh")

    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
        check("web UI deps", True, "fastapi + uvicorn")
    except ImportError:
        check("web UI deps", False, "run ./setup.sh")

    try:
        import PIL  # noqa: F401
        check("pillow", True, "burns timecodes into frames (--label)", required=False)
    except ImportError:
        check("pillow", False, "optional — --label will be skipped", required=False)

    from . import jira_api
    from .config import ENV_PATH

    check("jira site", bool(cfg.jira_base_url),
          cfg.jira_base_url or "not set — run: inpoint login", required=False)
    if jira_api.have_credentials():
        check("jira auth", True, f"API token for {jira_api.credentials()[0]} (headless)",
              required=False)
    else:
        check("jira auth", False,
              "no token — browser flow only. Run: inpoint login", required=False)

    check("asset root", cfg.asset_root.is_dir(), str(cfg.asset_root))
    check("downloads dir", cfg.downloads_dir.is_dir(), str(cfg.downloads_dir))
    check("config", ENV_PATH.is_file(),
          str(ENV_PATH) if ENV_PATH.is_file() else f"none yet — will be written to {ENV_PATH}",
          required=False)

    ok = all(c["ok"] for c in checks if c["required"])
    payload = {"jira_base_url": cfg.jira_base_url, "whisper_model": cfg.whisper_model,
               "asset_root": str(cfg.asset_root), "checks": checks, "ok": ok}
    emit(payload, args,
         [f"Jira   : {cfg.jira_base_url or '(not configured)'}",
          f"Model  : {cfg.whisper_model}",
          f"Assets : {cfg.asset_root}", ""] +
         [f"  {'ok  ' if c['ok'] else ('FAIL' if c['required'] else 'skip')}  "
          f"{c['name']:<16} {c['detail']}" for c in checks] +
         ["", "All good." if ok else "Something required is missing — run ./setup.sh"])
    return 0 if ok else 1


# ---------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    # `--json` is accepted both before and after the subcommand. The per-command
    # copy uses SUPPRESS so that `inpoint --json frames ...` isn't clobbered back to
    # False when the subparser fills in its defaults.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="machine-readable output")

    p = argparse.ArgumentParser(prog="inpoint", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--json", action="store_true", help="machine-readable output")
    sub = p.add_subparsers(dest="command", required=True)

    def add(parser, name, **kw):
        return parser.add_parser(name, parents=[common], **kw)

    # --- jira
    jp = sub.add_parser("jira", parents=[common], help="harvest a Jira ticket's attachments")
    jsub = jp.add_subparsers(dest="jira_command", required=True)

    fe = add(jsub, "fetch", help="read + download everything with an API token (no browser)")
    fe.add_argument("ticket")
    fe.add_argument("--force", action="store_true", help="re-download files already on disk")
    fe.add_argument("--reveal", action="store_true", help="reveal the folder in Finder")
    fe.set_defaults(func=cmd_jira_fetch)

    o = add(jsub, "open", help="print the ticket URL and the JS to read it")
    o.add_argument("ticket", help="ABC-123 or a full Jira URL")
    o.set_defaults(func=cmd_jira_open)

    i = add(jsub, "ingest", help="build folders from a fetched manifest")
    i.add_argument("--manifest", default="-", help="JSON file, inline JSON, or - for stdin")
    i.set_defaults(func=cmd_jira_ingest)

    c = add(jsub, "collect", help="file downloads from ~/Downloads into the ticket folder")
    c.add_argument("ticket")
    c.add_argument("--timeout", type=float, default=300.0, help="seconds to wait (default 300)")
    c.add_argument("--since", type=float, default=None, help="epoch cutoff for new files")
    c.add_argument("--only", action="append", help="restrict to these filenames/ids")
    c.add_argument("--reveal", action="store_true", help="reveal the folder in Finder")
    c.set_defaults(func=cmd_jira_collect)

    im = add(jsub, "import", help="move files you saved by hand into the ticket folder")
    im.add_argument("ticket")
    im.add_argument("files", nargs="+")
    im.set_defaults(func=cmd_jira_import)

    st = add(jsub, "status", help="what has been harvested so far")
    st.add_argument("ticket")
    st.set_defaults(func=cmd_jira_status)

    # --- transcribe
    t = add(sub, "transcribe", help="Whisper transcript (+ English translation)")
    t.add_argument("target", help="media file, folder, or ticket key")
    t.add_argument("--out", help="output directory (default: <ticket>/transcripts/<name>/)")
    t.add_argument("--model", help="override the Whisper model")
    t.add_argument("--language", help="force source language, e.g. hi, es (default: auto)")
    t.add_argument("--no-translate", action="store_true", help="skip the English pass")
    t.add_argument("--translate-model",
                   help="model for the English pass (turbo weights cannot translate)")
    t.add_argument("--force", action="store_true", help="redo even if a transcript exists")
    t.add_argument("--full-text", action="store_true", help="also print the timestamped text")
    t.set_defaults(func=cmd_transcribe)

    # --- transcript queries
    q = add(sub, "transcript", help="read back an existing transcript")
    q.add_argument("target")
    q.add_argument("--range", help="only this window, e.g. 00:01:20-00:01:40")
    q.add_argument("--search", help="only segments containing this text")
    q.add_argument("--translated", action="store_true", help="use the English translation")
    q.add_argument("--out", help="transcript directory override")
    q.set_defaults(func=cmd_transcript)

    # --- frames
    f = add(sub, "frames", help="extract stills from a video")
    f.add_argument("target", help="video file or ticket key")
    f.add_argument("--at", action="append", help="timestamps, repeatable or comma-separated")
    f.add_argument("--range", help="START-END, e.g. 00:01:20-00:01:40")
    f.add_argument("--every", help="sampling interval inside --range, e.g. 2s")
    f.add_argument("--count", type=int, help="N evenly spaced frames (in --range, or whole video)")
    f.add_argument("--scenes", action="store_true", help="grab every scene change instead")
    f.add_argument("--threshold", type=float, default=0.30, help="scene sensitivity 0-1")
    f.add_argument("--out", help="output directory")
    f.add_argument("--format", default="jpg", choices=["jpg", "png", "webp"])
    f.add_argument("--width", type=int, help="scale frames to this width")
    f.add_argument("--quality", type=int, default=2, help="jpg quality, 2=best 31=worst")
    f.add_argument("--label", action="store_true", help="burn the timestamp into each frame")
    f.add_argument("--prefix", default="frame")
    f.add_argument("--sheet", action="store_true", help="also build a contact sheet")
    f.add_argument("--sheet-cols", type=int, default=None)
    f.add_argument("--reveal", action="store_true")
    f.set_defaults(func=cmd_frames)

    lb = add(sub, "library", help="everything harvested so far, across all tickets")
    lb.add_argument("--project", help="only this project key, e.g. IS")
    lb.set_defaults(func=cmd_library)

    pr = add(sub, "probe", help="duration / codecs / resolution")
    pr.add_argument("target")
    pr.set_defaults(func=cmd_probe)

    sv = add(sub, "serve", help="run the drag-and-drop web UI")
    sv.add_argument("--host")
    sv.add_argument("--port", type=int)
    sv.add_argument("--open", action="store_true", help="open it in your browser")
    sv.set_defaults(func=cmd_serve)

    lg = add(sub, "login", help="store a Jira API token (interactive)")
    lg.add_argument("--site", help="https://yoursite.atlassian.net")
    lg.add_argument("--email", help="your Atlassian account email")
    lg.add_argument("--token", help="API token; omit to be prompted without echo")
    lg.set_defaults(func=cmd_login)

    lo = add(sub, "logout", help="clear the stored API token")
    lo.set_defaults(func=cmd_logout)

    dc = add(sub, "doctor", help="check that everything is installed")
    dc.set_defaults(func=cmd_doctor)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        note("\ninterrupted")
        return 130
    except (jira.JiraError, media.MediaError, transcribe.TranscribeError, ValueError) as exc:
        note(f"error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
