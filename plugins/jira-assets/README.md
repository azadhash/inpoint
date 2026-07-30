# asst

Harvest a Jira ticket's attachments into a tidy Desktop folder, transcribe the videos
locally, and pull out the exact frames you care about.

Two ways in:

- **Claude drives it.** A skill (`jira-assets`) teaches Claude Code the whole flow:
  open the ticket in your Chrome, download the attachments, file them, transcribe,
  and hand you back the stills for any stretch of timeline.
- **You drive it.** `asst serve --open` gives you a local page: drop a video, get a
  transcript, mark in/out, get frames.

Nothing is uploaded anywhere. Whisper runs on your own GPU.

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
translation alongside. Re-running reuses the existing transcript unless you pass
`--force`.

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
| `asst transcript <file>` | read a transcript back, filtered |
| `asst frames <file>` | extract stills |
| `asst probe <file>` | duration, codecs, resolution |
| `asst serve` | the web UI |

Add `--json` to any of them for machine-readable output — that's what Claude uses.
