"""Jira side: parse ticket refs, hand Chrome the snippets it needs, file the results.

Auth is your existing Chrome session -- we never hold a token. Claude opens the
ticket in a tab, runs `fetch_snippet()` (a same-origin REST call that inherits
your cookies) to get the attachment manifest, then runs `download_snippet()` to
make Chrome download each file. `ingest()` and `collect` do the filing.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .paths import (
    SUBDIR_FOR_KIND,
    TICKET_RE,
    ensure_ticket_dirs,
    find_ticket_dir,
    human_size,
    kind_for,
    safe_name,
    ticket_dir,
)

ISSUE_FIELDS = (
    "summary,status,project,description,attachment,issuetype,"
    "created,updated,reporter,assignee,priority,labels"
)


class JiraError(RuntimeError):
    pass


# ---------------------------------------------------------------- refs

def parse_ref(ref: str, cfg: Config) -> tuple[str, str]:
    """Accept 'ABC-123' or any Jira URL containing it. Returns (KEY, browse_url)."""
    ref = (ref or "").strip()
    if not ref:
        raise JiraError("no ticket given")

    base = cfg.jira_base_url
    if ref.startswith("http://") or ref.startswith("https://"):
        m = re.search(r"https?://([^/]+)", ref)
        if m:
            base = f"https://{m.group(1)}"

    if not base:
        raise JiraError(
            "No Jira site configured. Run `asst login`, or set JIRA_BASE_URL in your "
            "config — or just pass the full ticket URL instead of the key."
        )

    matches = TICKET_RE.findall(ref.upper())
    if not matches:
        raise JiraError(
            f"couldn't find a ticket key in {ref!r} -- expected something like ABC-123"
        )
    project, number = matches[-1]
    key = f"{project}-{number}"
    return key, f"{base}/browse/{key}"


def api_url(key: str, base: str) -> str:
    return f"{base.rstrip('/')}/rest/api/3/issue/{key}?fields={ISSUE_FIELDS}"


# ---------------------------------------------------------------- browser snippets

_FETCH_JS = r"""
await (async () => {
  const KEY = "__KEY__";
  const FIELDS = "__FIELDS__";
  const res = await fetch(`/rest/api/3/issue/${KEY}?fields=${FIELDS}`, {
    headers: { Accept: "application/json" },
    credentials: "same-origin",
  });
  if (!res.ok) {
    return {
      error: `HTTP ${res.status} fetching ${KEY}`,
      hint: (res.status === 401 || res.status === 403)
        ? "Not authenticated on this site -- log in to Jira in this tab and retry."
        : (res.status === 404 ? "No such issue, or you lack permission to see it." : ""),
    };
  }
  const data = await res.json();
  const f = data.fields || {};

  const flatten = (node, out) => {
    if (!node) return;
    if (Array.isArray(node)) { node.forEach(n => flatten(n, out)); return; }
    if (node.type === "text") out.push(node.text || "");
    if (node.type === "hardBreak") out.push("\n");
    if (node.type === "mention") out.push("@" + ((node.attrs || {}).text || "").replace(/^@/, ""));
    if (node.type === "media") out.push(`[attachment:${(node.attrs || {}).id || "?"}]`);
    if (node.type === "inlineCard") out.push((node.attrs || {}).url || "");
    if (node.content) flatten(node.content, out);
    if (["paragraph","heading","listItem","blockquote","codeBlock","rule",
         "mediaSingle","mediaGroup","tableRow","panel"].includes(node.type)) out.push("\n");
  };
  const asText = (adf) => {
    if (!adf) return "";
    if (typeof adf === "string") return adf;
    const out = []; flatten(adf, out);
    return out.join("").replace(/\n{3,}/g, "\n\n").trim();
  };

  return {
    key: data.key,
    url: `${location.origin}/browse/${data.key}`,
    project: { key: (f.project || {}).key, name: (f.project || {}).name },
    summary: f.summary || "",
    status: (f.status || {}).name || "",
    issuetype: (f.issuetype || {}).name || "",
    priority: (f.priority || {}).name || "",
    labels: f.labels || [],
    reporter: (f.reporter || {}).displayName || "",
    assignee: (f.assignee || {}).displayName || "",
    created: f.created || "",
    updated: f.updated || "",
    description: asText(f.description),
    attachments: (f.attachment || []).map(a => ({
      id: a.id,
      filename: a.filename,
      mimeType: a.mimeType || "",
      size: a.size || 0,
      created: a.created || "",
      author: (a.author || {}).displayName || "",
      content: a.content,
      thumbnail: a.thumbnail || "",
    })),
  };
})()
"""

_DOWNLOAD_JS = r"""
await (async () => {
  const ITEMS = __ITEMS__;
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const started = [];
  for (const item of ITEMS) {
    const a = document.createElement("a");
    a.href = item.url;
    a.download = item.filename;
    a.rel = "noopener";
    a.style.display = "none";
    document.body.appendChild(a);
    a.click();
    a.remove();
    started.push(item.filename);
    await sleep(700);
  }
  return { triggered: started.length, files: started };
})()
"""


def fetch_snippet(key: str) -> str:
    """JS that returns the issue manifest, run inside a logged-in Jira tab."""
    return _FETCH_JS.replace("__KEY__", key).replace("__FIELDS__", ISSUE_FIELDS).strip()


def download_snippet(plan_items: list[dict]) -> str:
    """JS that makes Chrome download each planned attachment."""
    items = [{"url": i["url"], "filename": i["filename"]} for i in plan_items]
    return _DOWNLOAD_JS.replace("__ITEMS__", json.dumps(items, ensure_ascii=False)).strip()


# ---------------------------------------------------------------- ingest

def build_plan(manifest: dict, cfg: Config) -> dict:
    """Turn a fetched manifest into folders on disk plus a per-attachment download plan."""
    if manifest.get("error"):
        raise JiraError(f"{manifest['error']}. {manifest.get('hint', '')}".strip())

    key = (manifest.get("key") or "").upper()
    if not key:
        raise JiraError("manifest has no issue key -- did the fetch snippet return an error?")

    project_key = (manifest.get("project") or {}).get("key") or key.rsplit("-", 1)[0]
    root = ticket_dir(cfg, project_key, key)
    dirs = ensure_ticket_dirs(root)

    plan: list[dict] = []
    used: set[str] = set()
    for att in manifest.get("attachments") or []:
        filename = safe_name(att.get("filename") or f"attachment-{att.get('id', 'x')}")
        kind = kind_for(filename, att.get("mimeType", ""))
        subdir = SUBDIR_FOR_KIND[kind]

        # Two attachments can legitimately share a filename on one issue.
        target_name, n = filename, 2
        while f"{subdir}/{target_name}" in used:
            stem, _, ext = filename.rpartition(".")
            target_name = f"{stem} ({n}).{ext}" if stem else f"{filename} ({n})"
            n += 1
        used.add(f"{subdir}/{target_name}")

        plan.append({
            "id": att.get("id"),
            "filename": target_name,
            "original_filename": att.get("filename"),
            "kind": kind,
            "mime": att.get("mimeType", ""),
            "size": int(att.get("size") or 0),
            "author": att.get("author", ""),
            "created": att.get("created", ""),
            "url": att.get("content"),
            "target": str(dirs[subdir] / target_name),
            "subdir": subdir,
        })

    ticket = {
        "schema": 1,
        "key": key,
        "project": manifest.get("project") or {"key": project_key},
        "summary": manifest.get("summary", ""),
        "status": manifest.get("status", ""),
        "issuetype": manifest.get("issuetype", ""),
        "priority": manifest.get("priority", ""),
        "labels": manifest.get("labels", []),
        "reporter": manifest.get("reporter", ""),
        "assignee": manifest.get("assignee", ""),
        "created": manifest.get("created", ""),
        "updated": manifest.get("updated", ""),
        "url": manifest.get("url") or f"{cfg.jira_base_url}/browse/{key}",
        "description": manifest.get("description", ""),
        "folder": str(root),
        "harvested_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "attachments": plan,
    }

    save_ticket(root, ticket)
    write_readme(root, ticket)
    return {"ticket": ticket, "folder": root, "dirs": dirs, "plan": plan}


def ticket_json_path(root: Path) -> Path:
    return Path(root) / "ticket.json"


def save_ticket(root: Path, ticket: dict) -> Path:
    path = ticket_json_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ticket, indent=2, ensure_ascii=False))
    return path


def load_ticket(cfg: Config, key_or_dir: str) -> tuple[Path, dict]:
    candidate = Path(key_or_dir).expanduser()
    root = candidate if candidate.is_dir() else None
    if root is None:
        key, _ = parse_ref(key_or_dir, cfg)
        root = find_ticket_dir(cfg, key)
    if root is None or not ticket_json_path(root).is_file():
        raise JiraError(
            f"no harvested ticket found for {key_or_dir!r}. Run `asst jira ingest` first."
        )
    return root, json.loads(ticket_json_path(root).read_text())


def write_readme(root: Path, ticket: dict) -> Path:
    lines = [
        f"# {ticket['key']} — {ticket.get('summary', '')}".rstrip(" —"),
        "",
        f"- **Link**: {ticket.get('url', '')}",
        f"- **Status**: {ticket.get('status', '—')}"
        f"  ·  **Type**: {ticket.get('issuetype', '—')}"
        f"  ·  **Priority**: {ticket.get('priority', '—')}",
        f"- **Reporter**: {ticket.get('reporter', '—')}"
        f"  ·  **Assignee**: {ticket.get('assignee', '—')}",
        f"- **Harvested**: {ticket.get('harvested_at', '')}",
        "",
    ]
    if ticket.get("description"):
        lines += ["## Description", "", ticket["description"].strip(), ""]

    atts = ticket.get("attachments") or []
    lines += [f"## Attachments ({len(atts)})", ""]
    if not atts:
        lines.append("_None._")
    else:
        lines += ["| File | Kind | Size | Saved to |", "| --- | --- | --- | --- |"]
        for a in atts:
            saved = a.get("saved_to")
            where = f"`{a['subdir']}/{Path(saved).name}`" if saved else "_not downloaded_"
            lines.append(
                f"| {a['filename']} | {a['kind']} | {human_size(a['size'])} | {where} |"
            )
    lines.append("")

    path = Path(root) / "README.md"
    path.write_text("\n".join(lines))
    return path


def mark_collected(root: Path, ticket: dict, moved: list[dict]) -> dict:
    """Record where each attachment actually landed."""
    by_target = {m["target"]: m for m in moved}
    for att in ticket.get("attachments") or []:
        hit = by_target.get(att.get("target"))
        if hit:
            att["saved_to"] = hit["saved_to"]
            att["bytes"] = hit["bytes"]
    save_ticket(root, ticket)
    write_readme(root, ticket)
    return ticket


def media_files(root: Path, ticket: dict | None = None) -> list[Path]:
    """Every downloaded video/audio file in a ticket folder."""
    out: list[Path] = []
    for sub in ("videos", "audio"):
        d = Path(root) / sub
        if d.is_dir():
            out += sorted(p for p in d.iterdir() if p.is_file() and not p.name.startswith("."))
    return out
