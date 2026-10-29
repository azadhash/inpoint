# inpoint

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
- **You drive it.** `inpoint serve --open` gives you a local page: drop a video, get a
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
| macOS 26+ | Apple's on-device speech engine | English + 9 languages, no download, fastest |
| macOS, Apple Silicon | GPU via [MLX](https://github.com/ml-explore/mlx) | ~10× real time; used for other languages and translation |
| macOS Intel · Linux · Windows | CPU via openai-whisper | Same output, several × slower |

The backend is picked automatically; `INPOINT_WHISPER_BACKEND=apple\|mlx\|openai` forces one.
Apple's engine needs the Xcode Command Line Tools (`xcode-select --install`) to build
a tiny helper once. It assumes English unless you pass `--language`; Hindi and other
unsupported languages go to Whisper automatically.
Model weights (~0.9 GB) download on your first transcription. The whole install is
about 1.4 GB on Apple Silicon; CPU platforms add ~0.6 GB for PyTorch.

Frame extraction, the Jira side, and the web UI are platform-independent.

---

## Install

As a Claude Code plugin:

```
/plugin install inpoint --marketplace azadhash/inpoint
```

Or standalone, without Claude:

```bash
./setup.sh
```

Either way, `bin/inpoint` bootstraps its own virtualenv on first run. You need `uv` and
`ffmpeg` (`brew install uv ffmpeg`). The Whisper model (~0.9 GB) downloads the first
time you transcribe something.

## Connect to Jira

Two options — pick either.

**API token (recommended).** Works headlessly, on any OS, in CI:

```bash
inpoint login
```

It walks you through creating a token at
[id.atlassian.com](https://id.atlassian.com/manage-profile/security/api-tokens),
verifies it against your site, and saves it to `.env` with owner-only permissions.
Then one command does everything:

```bash
inpoint jira fetch ABC-123
```

**Browser session (no token).** If you have the Claude in Chrome extension, just ask
Claude to grab the ticket — it drives your logged-in Chrome and nothing is stored.
See "Harvesting a ticket" below.

Check where you stand:

```bash
inpoint doctor          # look at the `jira auth` line
inpoint logout          # clear a stored token
```

Other settings live in `.env` — Jira site, where folders get created, which Whisper
model.

Harvests are shared state on disk, so a second Claude session (or a second terminal,
or the web UI) sees everything the first one produced. Start with:

```bash
inpoint library            # every ticket harvested so far, and what's been done to it
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
inpoint jira fetch ISP-482
```

Without one, auth is your existing Chrome session — nothing is stored. Claude opens
the ticket, reads it with a same-origin REST call that inherits your cookies, and
makes Chrome download each attachment. Just ask:

> Grab the assets from ISP-482

Or drive the browser flow yourself:

```bash
inpoint jira open ISP-482              # prints the URL + a JS snippet to paste in devtools
# paste the snippet, save the JSON it returns to /tmp/m.json
inpoint jira ingest --manifest /tmp/m.json
# paste the download snippet it prints back into the same tab
inpoint jira collect ISP-482
inpoint jira status ISP-482
```

If something doesn't come down cleanly, save it by hand and file it with
`inpoint jira import ISP-482 ~/Downloads/whatever.mp4`.

---

## Transcribing

```bash
inpoint transcribe ISP-482                    # every video/audio file on the ticket
inpoint transcribe ~/Desktop/demo.mov         # or any file
inpoint transcribe demo.mov --language hi     # force the source language
inpoint transcribe demo.mov --no-translate    # skip the English pass
```

Auto-detects the language, and when it isn't English also writes an English
translation alongside as `transcript.en.*`. Re-running reuses the existing transcript
unless you pass `--force`.

> **On translation.** The default model, `whisper-large-v3-turbo-8bit`, is distilled for
> transcription only — asked to translate, it silently returns the source language
> rather than failing. So the English pass uses a separate non-turbo model
> (`whisper-large-v3-8bit`, ~1.7 GB), fetched the first time you transcribe something that
> isn't in English. Override it with `WHISPER_TRANSLATE_MODEL`, choose per-run with
> `--translate-model`, or skip the whole thing with `--no-translate`. If translation
> can't run, the transcript is still written and the reason lands in
> `transcript.json` under `translation_note`.

Read one back:

```bash
inpoint transcript demo.mov --range 00:01:20-00:01:40
inpoint transcript demo.mov --search "checkout"
inpoint transcript demo.mov --translated
```

## Frames

```bash
inpoint frames demo.mov --at 00:01:23                  # one still
inpoint frames demo.mov --at 1:23,1:40,2:05            # several
inpoint frames demo.mov --range 00:01:20-00:01:40 --every 2s
inpoint frames demo.mov --range 00:01:20-00:01:40 --count 8
inpoint frames demo.mov --scenes --threshold 0.3       # every scene change
inpoint frames demo.mov --range 1:20-1:40 --sheet      # + a contact sheet
```

Useful extras: `--label` burns the timecode into each frame, `--width 1280` scales
them down, `--format png` for lossless, `--reveal` opens the folder in Finder.

When a transcript exists, each frame comes back annotated with what was being said
at that moment.

## The web UI

```bash
inpoint serve --open
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
| `inpoint login` / `inpoint logout` | store or clear a Jira API token |
| `inpoint jira fetch <KEY>` | read + download everything, no browser |
| `inpoint doctor` | check ffmpeg / whisper / paths / auth |
| `inpoint library` | every harvested ticket, with transcript / frame status |
| `inpoint jira open <KEY>` | ticket URL + the fetch snippet |
| `inpoint jira ingest --manifest <file\|->` | build the folders, print the download plan |
| `inpoint jira collect <KEY>` | file what Chrome downloaded |
| `inpoint jira import <KEY> <files…>` | file something you saved by hand |
| `inpoint jira status <KEY>` | what's been harvested |
| `inpoint transcribe <file\|KEY>` | Whisper transcript + English translation |
| `inpoint library` | every harvested ticket, with transcript / frame status |
| `inpoint transcript <file>` | read a transcript back, filtered |
| `inpoint frames <file>` | extract stills |
| `inpoint probe <file>` | duration, codecs, resolution |
| `inpoint serve` | the web UI |

Add `--json` to any of them for machine-readable output — that's what Claude uses.

## Configuration

`inpoint login` writes the first three; the rest are optional. Config lives at
`~/.config/inpoint/.env`, outside the plugin so it survives plugin updates.

| Variable | Default | What it does |
| --- | --- | --- |
| `JIRA_BASE_URL` | — | your Atlassian site |
| `JIRA_EMAIL` / `JIRA_API_TOKEN` | — | headless auth; without them the browser flow is used |
| `ASSET_ROOT` | `~/Desktop` | where ticket folders are created |
| `DOWNLOADS_DIR` | `~/Downloads` | where the browser flow looks for downloads |
| `WHISPER_MODEL` | `whisper-large-v3-turbo-8bit` | transcription weights; `-q4` halves it again |
| `WHISPER_TRANSLATE_MODEL` | `whisper-large-v3-8bit` | weights for the English pass |
| `INPOINT_WHISPER_BACKEND` | auto | force `apple`, `mlx` or `openai` |
| `WEB_HOST` / `WEB_PORT` | `127.0.0.1:8765` | web UI bind address |

## What it doesn't do

- Jira Server / Data Center — Cloud REST v3 only.
- Attachments embedded in a description via Atlassian's media service rather than
  added as real attachments; those aren't in the issue's attachment list, so save
  them by hand and use `inpoint jira import`.
- Speaker diarisation — Whisper gives you the words, not who said them.
- Bulk harvesting across a JQL query; it's one ticket at a time.
