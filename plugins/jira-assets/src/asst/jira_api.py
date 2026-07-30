"""Headless Jira access with an API token.

This is the portable path: no browser, no extension, works on any OS and in CI.
The Chrome-session flow in jira.py stays as the zero-setup alternative.

Set these in .env (or the environment):
    JIRA_EMAIL=you@company.com
    JIRA_API_TOKEN=<from https://id.atlassian.com/manage-profile/security/api-tokens>
"""

from __future__ import annotations

import base64
import os
import re
from pathlib import Path
from typing import Callable

from .config import Config
from .jira import ISSUE_FIELDS, JiraError

Progress = Callable[[str, int, int], None]  # filename, bytes_done, bytes_total


class JiraAuthError(JiraError):
    pass


def credentials() -> tuple[str, str] | None:
    email = os.getenv("JIRA_EMAIL", "").strip()
    token = os.getenv("JIRA_API_TOKEN", "").strip()
    return (email, token) if email and token else None


def have_credentials() -> bool:
    return credentials() is not None


class JiraApi:
    """Minimal Jira Cloud REST client -- reads issues, streams attachments."""

    def __init__(self, cfg: Config, *, email: str | None = None, token: str | None = None):
        creds = (email, token) if (email and token) else credentials()
        if not creds:
            raise JiraAuthError(
                "No API token configured. Either set JIRA_EMAIL and JIRA_API_TOKEN in "
                ".env (get one at https://id.atlassian.com/manage-profile/security/api-tokens), "
                "or use the browser flow: asst jira open <KEY>"
            )
        self.base = cfg.jira_base_url.rstrip("/")
        self.email, self.token = creds
        basic = base64.b64encode(f"{self.email}:{self.token}".encode()).decode()
        self.auth_header = f"Basic {basic}"

    # ------------------------------------------------------------------ http

    def _client(self):
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise JiraError("httpx is not installed -- run ./setup.sh") from exc
        return httpx.Client(timeout=60.0, follow_redirects=False)

    def _get_json(self, path: str) -> dict:
        import httpx

        url = f"{self.base}{path}"
        with self._client() as client:
            try:
                res = client.get(url, headers={
                    "Authorization": self.auth_header, "Accept": "application/json",
                })
            except httpx.HTTPError as exc:
                raise JiraError(f"could not reach {self.base}: {exc}") from exc

        if res.status_code in (401, 403):
            raise JiraAuthError(
                f"Jira rejected the credentials for {self.email} at {self.base} "
                f"(HTTP {res.status_code})."
            )
        if res.status_code == 404:
            raise JiraError("No such issue, or your account cannot see it.")
        if res.status_code >= 400:
            raise JiraError(f"HTTP {res.status_code} from Jira: {res.text[:200]}")
        return res.json()

    def whoami(self) -> dict:
        """Verify the credentials and return the account they belong to."""
        return self._get_json("/rest/api/3/myself")

    # ------------------------------------------------------------------ issue

    def issue(self, key: str) -> dict:
        """Fetch an issue and return it in the same shape as the browser snippet."""
        data = self._get_json(f"/rest/api/3/issue/{key}?fields={ISSUE_FIELDS}")
        f = data.get("fields") or {}
        return {
            "key": data.get("key"),
            "url": f"{self.base}/browse/{data.get('key')}",
            "project": {
                "key": (f.get("project") or {}).get("key"),
                "name": (f.get("project") or {}).get("name"),
            },
            "summary": f.get("summary") or "",
            "status": (f.get("status") or {}).get("name") or "",
            "issuetype": (f.get("issuetype") or {}).get("name") or "",
            "priority": (f.get("priority") or {}).get("name") or "",
            "labels": f.get("labels") or [],
            "reporter": (f.get("reporter") or {}).get("displayName") or "",
            "assignee": (f.get("assignee") or {}).get("displayName") or "",
            "created": f.get("created") or "",
            "updated": f.get("updated") or "",
            "description": adf_to_text(f.get("description")),
            "attachments": [
                {
                    "id": a.get("id"),
                    "filename": a.get("filename"),
                    "mimeType": a.get("mimeType") or "",
                    "size": a.get("size") or 0,
                    "created": a.get("created") or "",
                    "author": (a.get("author") or {}).get("displayName") or "",
                    # Rewrite the api.atlassian.com gateway form to the site form so
                    # the same manifest works for the browser flow too.
                    "content": self._site_url(a.get("content") or "", a.get("id")),
                }
                for a in (f.get("attachment") or [])
            ],
        }

    def _site_url(self, content: str, attachment_id: str | None) -> str:
        if content.startswith(self.base):
            return content
        if attachment_id:
            return f"{self.base}/rest/api/3/attachment/content/{attachment_id}"
        return content

    # ------------------------------------------------------------------ files

    def download(self, url: str, dest: Path, *, on_progress: Progress | None = None) -> Path:
        """Stream one attachment to disk.

        Jira 302s to a signed media CDN. The signature is the credential there, so
        the Authorization header is dropped on the cross-host hop -- sending it
        would make the CDN reject the request.
        """
        import httpx

        dest.parent.mkdir(parents=True, exist_ok=True)
        headers = {"Authorization": self.auth_header, "Accept": "*/*"}
        current = url

        with self._client() as client:
            for _ in range(6):
                try:
                    res = client.get(current, headers=headers)
                except httpx.HTTPError as exc:
                    raise JiraError(f"download failed for {dest.name}: {exc}") from exc

                if res.status_code in (301, 302, 303, 307, 308):
                    location = res.headers.get("location")
                    if not location:
                        raise JiraError(f"redirect without a Location for {dest.name}")
                    current = str(httpx.URL(current).join(location))
                    if httpx.URL(current).host != httpx.URL(url).host:
                        headers = {"Accept": "*/*"}
                    continue

                if res.status_code in (401, 403):
                    raise JiraAuthError(f"not allowed to download {dest.name} (HTTP {res.status_code})")
                if res.status_code >= 400:
                    raise JiraError(f"HTTP {res.status_code} downloading {dest.name}")

                total = int(res.headers.get("content-length") or 0)
                tmp = dest.with_suffix(dest.suffix + ".part")
                written = 0
                with tmp.open("wb") as fh:
                    for chunk in res.iter_bytes(1024 * 256):
                        fh.write(chunk)
                        written += len(chunk)
                        if on_progress:
                            on_progress(dest.name, written, total)
                tmp.replace(dest)
                return dest

        raise JiraError(f"too many redirects downloading {dest.name}")


# ---------------------------------------------------------------- ADF

def adf_to_text(node) -> str:
    """Flatten Atlassian Document Format into readable plain text."""
    if not node:
        return ""
    if isinstance(node, str):
        return node

    out: list[str] = []
    _walk(node, out)
    return re.sub(r"\n{3,}", "\n\n", "".join(out)).strip()


_BLOCK_TYPES = {
    "paragraph", "heading", "listItem", "blockquote", "codeBlock",
    "rule", "mediaSingle", "mediaGroup", "tableRow", "panel",
}


def _walk(node, out: list[str]) -> None:
    if isinstance(node, list):
        for child in node:
            _walk(child, out)
        return
    if not isinstance(node, dict):
        return

    kind = node.get("type")
    attrs = node.get("attrs") or {}
    if kind == "text":
        out.append(node.get("text") or "")
    elif kind == "hardBreak":
        out.append("\n")
    elif kind == "mention":
        out.append("@" + str(attrs.get("text") or "").lstrip("@"))
    elif kind == "media":
        out.append(f"[attachment:{attrs.get('id', '?')}]")
    elif kind == "inlineCard":
        out.append(str(attrs.get("url") or ""))

    if node.get("content"):
        _walk(node["content"], out)
    if kind in _BLOCK_TYPES:
        out.append("\n")
