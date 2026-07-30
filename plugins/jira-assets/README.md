# asst

Someone attaches a two-minute screen recording to a bug ticket. Somewhere in it is the
moment the thing breaks. This gets you the words and the picture, at the timestamp you
care about, without scrubbing.

Point it at a Jira ticket and it downloads the attachments into a tidy folder,
transcribes the videos, and pulls stills from any stretch of timeline — each one
labelled with what was being said at that instant.

Two ways in:

- **Claude drives it.** The bundled skill teaches Claude Code the whole flow. Ask
  *"grab the assets from ABC-123 and tell me what's in the video"* and it harvests,
  transcribes, extracts frames, and reads them back to you.
- **You drive it.** `asst serve --open` gives you a local page: drop a video, get a
  transcript, mark in and out, get frames.

**Nothing is uploaded anywhere.** Transcription runs on your machine; the only network
calls are to your own Jira and a one-time model download from Hugging Face.

---

## Requirements

```bash
brew install uv ffmpeg
```

| Platform | Transcription | Notes |
| --- | --- | --- |
| macOS, Apple Silicon | GPU via [MLX](https://github.com/ml-explore/mlx) | ~10× real time |
| macOS Intel · Linux · Windows | CPU via openai-whisper | Same output, several × slower |

The backend is picked automatically; `ASST_WHISPER_BACKEND=mlx\|openai` forces one.
Model weights (~1.5 GB) download on your first transcription.

Frame extraction, the Jira side, and the web UI are platform-independent.

---

## Install

As a Claude Code plugin (see the repo root README), or standalone:

```bash
./setup.sh
```

Either way, `bin/asst` bootstraps its own virtualenv on first run. You need `uv` and
`ffmpeg` (`brew install uv ffmpeg`). The Whisper model (~1.5 GB) downloads the first
time you transcribe something.

## Connect to Jira

Two options — pick either.

**API token (recommended).** Works headlessly, on any OS, in CI:

```bash
asst login
```

It walks you through creating a token at
[id.atlassian.com](https://id.atlassian.com/manage-profile/security/api-tokens),
verifies it against your site, and saves it to `.env` with owner-only permissions.
Then one command does everything:

```bash
asst jira fetch ABC-123
```

**Browser session (no token).** If you have the Claude in Chrome extension, just ask
Claude to grab the ticket — it drives your logged-in Chrome and nothing is stored.
See "Harvesting a ticket" below.

Check where you stand:

```bash
asst doctor          # look at the `jira auth` line
asst logout          # clear a stored token
```

Other settings live in `.env` — Jira site, where folders get created, which Whisper
model.

Harvests are shared state on disk, so a second Claude session (or a second terminal,
or the web UI) sees everything the first one produced. Start with:

```bash
asst library            # every ticket harvested so far, and what's been done to it
```

---

## The folder layout

Everything for a ticket lands under `~/Desktop/<PROJECT>/<TICKET>/`:

```
~/Desktop/ISP/ISP-482/
├── ticket.json                     summary, status, description, attachment manifest
├── README.md                       the same thing, readable
├── images/         screenshot.png
├── videos/         bug-repro.mp4
├── audio/          voice-note.m4a
├── files/          logs.txt
├── transcripts/
│   └── bug-repro/
│       ├── transcript.json         segments + word-level timestamps
│       ├── transcript.srt / .vtt   subtitles
│       ├── transcript.txt          plain paragraphs
│       ├── transcript.timed.txt    [00:01:23.500] one line per segment
│       └── transcript.en.*         the English translation, if the source wasn't English
└── frames/
    └── bug-repro/
        ├── frame_00-01-23.500.jpg
        └── contact-sheet.jpg
```

---

## Harvesting a ticket

With a token, it's one command:

```bash
asst jira fetch ISP-482
```

Without one, auth is your existing Chrome session — nothing is stored. Claude opens
the ticket, reads it with a same-origin REST call that inherits your cookies, and
makes Chrome download each attachment. Just ask:

> Grab the assets from ISP-482

Or drive the browser flow yourself:

```bash
asst jira open ISP-482              # prints the URL + a JS snippet to paste in devtools
# paste the snippet, save the JSON it returns to /tmp/m.json
asst jira ingest --manifest /tmp/m.json
# paste the download snippet it prints back into the same tab
asst jira collect ISP-482
asst jira status ISP-482
```

If something doesn't come down cleanly, save it by hand and file it with
`asst jira import ISP-482 ~/Downloads/whatever.mp4`.

---

## Transcribing

```bash
asst transcribe ISP-482                    # every video/audio file on the ticket
asst transcribe ~/Desktop/demo.mov         # or any file
asst transcribe demo.mov --language hi     # force the source language
asst transcribe demo.mov --no-translate    # skip the English pass
```

Auto-detects the language, and when it isn't English also writes an English
translation alongside as `transcript.en.*`. Re-running reuses the existing transcript
unless you pass `--force`.

> **On translation.** The default model, `whisper-large-v3-turbo`, is distilled for
> transcription only — asked to translate, it silently returns the source language
> rather than failing. So the English pass uses a separate non-turbo model
> (`whisper-large-v3`, ~3 GB), fetched the first time you transcribe something that
> isn't in English. Override it with `WHISPER_TRANSLATE_MODEL`, choose per-run with
> `--translate-model`, or skip the whole thing with `--no-translate`. If translation
> can't run, the transcript is still written and the reason lands in
> `transcript.json` under `translation_note`.

Read one back:

```bash
asst transcript demo.mov --range 00:01:20-00:01:40
asst transcript demo.mov --search "checkout"
asst transcript demo.mov --translated
```

## Frames

```bash
asst frames demo.mov --at 00:01:23                  # one still
asst frames demo.mov --at 1:23,1:40,2:05            # several
asst frames demo.mov --range 00:01:20-00:01:40 --every 2s
asst frames demo.mov --range 00:01:20-00:01:40 --count 8
asst frames demo.mov --scenes --threshold 0.3       # every scene change
asst frames demo.mov --range 1:20-1:40 --sheet      # + a contact sheet
```

Useful extras: `--label` burns the timecode into each frame, `--width 1280` scales
them down, `--format png` for lossless, `--reveal` opens the folder in Finder.

When a transcript exists, each frame comes back annotated with what was being said
at that moment.

## The web UI

```bash
asst serve --open
```

Drop a video anywhere on the page. It transcribes, then you can:

- click any transcript line to jump the video there
- `i` and `o` to mark in and out, `space` to play, `←`/`→` to step a frame
- click `⟦⟧` on a transcript line to set the range to exactly that sentence
- extract every N seconds, N evenly spaced frames, a single frame at the playhead,
  or every scene change in the range
- download the lot as a zip

Everything it writes goes to the same folders the CLI uses, so tickets and uploads
share one library.

---

## Command reference

| Command | What it does |
| --- | --- |
| `asst login` / `asst logout` | store or clear a Jira API token |
| `asst jira fetch <KEY>` | read + download everything, no browser |
| `asst doctor` | check ffmpeg / whisper / paths / auth |
| `asst library` | every harvested ticket, with transcript / frame status |
| `asst jira open <KEY>` | ticket URL + the fetch snippet |
| `asst jira ingest --manifest <file\|->` | build the folders, print the download plan |
| `asst jira collect <KEY>` | file what Chrome downloaded |
| `asst jira import <KEY> <files…>` | file something you saved by hand |
| `asst jira status <KEY>` | what's been harvested |
| `asst transcribe <file\|KEY>` | Whisper transcript + English translation |
| `asst library` | every harvested ticket, with transcript / frame status |
| `asst transcript <file>` | read a transcript back, filtered |
| `asst frames <file>` | extract stills |
| `asst probe <file>` | duration, codecs, resolution |
| `asst serve` | the web UI |

Add `--json` to any of them for machine-readable output — that's what Claude uses.

## Configuration

`asst login` writes the first three; the rest are optional. Config lives at
`~/.config/asst/.env`, outside the plugin so it survives plugin updates.

| Variable | Default | What it does |
| --- | --- | --- |
| `JIRA_BASE_URL` | — | your Atlassian site |
| `JIRA_EMAIL` / `JIRA_API_TOKEN` | — | headless auth; without them the browser flow is used |
| `ASSET_ROOT` | `~/Desktop` | where ticket folders are created |
| `DOWNLOADS_DIR` | `~/Downloads` | where the browser flow looks for downloads |
| `WHISPER_MODEL` | `whisper-large-v3-turbo` | transcription weights |
| `WHISPER_TRANSLATE_MODEL` | `whisper-large-v3` | weights for the English pass |
| `ASST_WHISPER_BACKEND` | auto | force `mlx` or `openai` |
| `WEB_HOST` / `WEB_PORT` | `127.0.0.1:8765` | web UI bind address |

## What it doesn't do

- Jira Server / Data Center — Cloud REST v3 only.
- Attachments embedded in a description via Atlassian's media service rather than
  added as real attachments; those aren't in the issue's attachment list, so save
  them by hand and use `asst jira import`.
- Speaker diarisation — Whisper gives you the words, not who said them.
- Bulk harvesting across a JQL query; it's one ticket at a time.
