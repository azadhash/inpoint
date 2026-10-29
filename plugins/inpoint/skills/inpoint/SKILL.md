---
name: inpoint
description: Harvest a Jira ticket's attachments (images, videos, files) into a Desktop folder using the user's logged-in Chrome, then transcribe the videos locally with Whisper and extract frames at any timestamp or range. Use whenever the user mentions a Jira ticket key (like ABC-123) together with downloading, grabbing, or looking at its attachments; or asks to transcribe a video, translate what is said in it, or get the stills/screenshots from a stretch of a video's timeline. Also use to look at assets a previous session already harvested — "what do we have for ABC-123", "what was said in that video", "show me the screenshots from that ticket" — via `inpoint library`.
---

# Jira assets: download, transcribe, extract frames

`inpoint` is on your PATH whenever this plugin is enabled — call it directly.
Add `--json` to every call; the output is meant for you.

Everything runs on this machine. Whisper transcription uses the Apple Silicon GPU;
nothing is uploaded to any service.

## Start here if something was harvested already

Harvests are shared state on disk — any session, any working directory, sees the same
thing. Before harvesting anything, check what's already there:

```bash
inpoint library --json
```

Lists every harvested ticket with its folder, file counts, and per-video transcript
and frame status. `--project IS` narrows it. If the ticket you want is already listed,
skip Part 1 entirely — go straight to reading the transcript or extracting frames.

You never need this tool to *read* the results. Everything is plain files under
`~/Desktop/<PROJECT>/<TICKET>/` — use Read on `transcript.timed.txt` for the words
with timestamps, or on any `frames/**/frame_*.jpg` to see the screen. Reach for `inpoint`
when you want to transcribe something new, pull frames at new timestamps, or query a
transcript by range or keyword.

Two caveats when sessions overlap: don't run `inpoint transcribe` on a file another
session is already transcribing (the second run wins and rewrites the sidecar), and
only one `inpoint serve` can hold port 8765 — pass `--port` for a second one.

## Where things land

```
~/Desktop/<PROJECT_KEY>/<TICKET_KEY>/
  ticket.json  README.md
  images/  videos/  audio/  files/
  transcripts/<video-stem>/transcript.{json,srt,vtt,txt,timed.txt}  (+ transcript.en.* )
  frames/<video-stem>/frame_00-01-23.500.jpg
```

## Part 1 — harvest a ticket

There are two ways in. Check which one is available first:

```bash
inpoint doctor --json
```

Look at the `jira auth` check. If it reports an API token, use **1A** — one command,
no browser. If not, use **1B**, the browser flow.

### 1A — with an API token (preferred)

```bash
inpoint jira fetch ABC-123 --json
```

That reads the issue, creates the folders, and downloads every attachment in one
step. Returns `{folder, downloaded, failed}`. Nothing else to do — skip to Part 2.

If it fails with an auth error, tell the user to run `inpoint login` **themselves in
their terminal** — it prompts for the token without echoing it. Never ask them to
paste a token into the chat, and never run `inpoint login --token ...` on their behalf
with a value they sent you.

### 1B — with the browser session (no token needed)

Requires the Claude in Chrome extension. Four steps:

**1. Get the URL and the fetch snippet.**

```bash
inpoint jira open ABC-123 --json
```

Returns `{key, url, fetch_snippet}`.

**2. Open the ticket and read it.** Use the claude-in-chrome tools — call
`tabs_context_mcp` first, then `tabs_create_mcp` with the returned `url`, then
`javascript_tool` with `fetch_snippet` verbatim.

The snippet returns the issue manifest as JSON. If it comes back with
`{error: "HTTP 401…"}` or `403`, the user isn't logged into Jira in that browser —
tell them to log in, then retry. Don't try to work around it.

**3. Create the folders.** Write the manifest JSON to a file in your scratchpad,
then:

```bash
inpoint jira ingest --manifest /path/to/manifest.json --json
```

Returns `{folder, plan, download_snippet, since_epoch}`. `plan` lists every
attachment with its kind and destination. Nothing has been downloaded yet.

**4. Download and file.** Run `download_snippet` verbatim in the same Jira tab with
`javascript_tool` — it makes Chrome download each attachment. Then:

```bash
inpoint jira collect ABC-123 --json
```

This watches `~/Downloads` and moves each file into the right subfolder. It reports
`moved` and `missing`. If anything is missing, re-run the download snippet and
collect again; if it still won't come, ask the user to save it manually and use
`inpoint jira import ABC-123 <path>`.

Check state at any time with `inpoint jira status ABC-123 --json`.

## Part 2 — transcribe

```bash
inpoint transcribe ABC-123 --json
```

Accepts a ticket key (does every video and audio file on it), a folder, or a single
file path. Auto-detects the language and also writes an English translation when the
source isn't English. Already-transcribed files are reused unless you pass `--force`.

The first run downloads the Whisper model (~1.5 GB) — say so before starting, and
expect it to take a few minutes. After that, transcription runs roughly 10× faster
than real time.

Read a transcript back:

```bash
inpoint transcript <file> --json                                  # everything
inpoint transcript <file> --range 00:01:20-00:01:40 --json        # just that window
inpoint transcript <file> --search "checkout" --json              # find a moment
inpoint transcript <file> --translated --json                     # the English version
```

## Part 3 — frames

```bash
inpoint frames <file> --at 00:01:23 --json                        # one still
inpoint frames <file> --at 1:23,1:40,2:05 --json                  # several
inpoint frames <file> --range 1:20-1:40 --every 2s --json         # sampled through a range
inpoint frames <file> --range 1:20-1:40 --count 8 --json          # 8 evenly spaced
inpoint frames <file> --scenes --json                             # every scene change
```

Each frame comes back with its `path`, its `label` (the timecode) and, when a
transcript exists, `said` — what was being spoken at that exact moment.

**Then read the images.** Use the Read tool on the returned `path` values so you can
actually describe what is on screen. That is usually the point of the request.

Useful flags: `--label` burns the timecode into the frame, `--width 1280` scales
down, `--sheet` also builds a contact sheet, `--format png` for lossless.

## Typical request → what to do

> "Grab everything from ABC-123 and tell me what's in the video"

Harvest (part 1) → transcribe (part 2) → summarize the transcript → offer to pull
frames from any moment.

> "What's on screen between 1:20 and 1:40?"

`inpoint transcript <file> --range 1:20-1:40` for the words, then
`inpoint frames <file> --range 1:20-1:40 --count 6`, then Read each returned image and
describe it alongside what is being said.

> "Screenshot the moment they mention the error"

`inpoint transcript <file> --search "error" --json` to find the timestamp, then
`inpoint frames <file> --at <that timestamp>`, then Read the image.

## Rules

- Never guess a ticket key. If the user is vague about which ticket, ask.
- Don't try to download Jira attachments with `curl` or `WebFetch` — they need the
  browser session and will come back as HTML login pages.
- Avoid extracting more than ~40 frames in one go unless asked; sample a range and
  narrow down from there.
- `inpoint doctor --json` diagnoses a broken setup. `./setup.sh` fixes most of it.
- There is also a web UI (`inpoint serve --open`) for the user to drive by hand — mention
  it when they want to scrub a timeline themselves rather than name timestamps.
