# asst-tools

A Claude Code plugin marketplace. One plugin so far:

**[jira-assets](plugins/jira-assets)** — pull a Jira ticket's images and videos into a
tidy folder, transcribe the videos locally with Whisper, and grab frames from any
moment on the timeline. Ask Claude *"grab the assets from ABC-123 and tell me what's
in the video"* and it does the whole run.

Nothing is uploaded anywhere. Transcription runs on your own machine.

---

## Install

```
/plugin marketplace add azad-betacraft/asst-tools
/plugin install jira-assets@asst-tools
/reload-plugins
```

Or straight from a local checkout, no GitHub needed:

```
/plugin marketplace add /path/to/asst-tools
/plugin install jira-assets@asst-tools
```

The first `asst` call builds its own virtualenv — expect a couple of minutes once,
then it's instant. Everything after that is `asst ...`, already on your PATH.

### Prerequisites

```bash
brew install uv ffmpeg
```

macOS on Apple Silicon gets GPU transcription via MLX. Elsewhere it falls back to
CPU Whisper — same output, slower.

### Connect to Jira

```bash
asst login
```

Prompts for your site, your Atlassian email, and an
[API token](https://id.atlassian.com/manage-profile/security/api-tokens); verifies it
before saving to a `.env` that only you can read. After that:

```bash
asst jira fetch ABC-123
```

If you'd rather not create a token and you have the Claude in Chrome extension,
skip `asst login` entirely — Claude will drive your logged-in browser instead.

---

## Other ways to share this

| Route | Good for | Cost |
| --- | --- | --- |
| **Plugin marketplace** (this repo) | Claude Code users on your team | push to GitHub, they run two commands |
| `git clone` + `./setup.sh` | people who want the CLI without Claude | no plugin machinery |
| Private GitHub repo | keeping it internal | same commands; they need repo access |
| `--plugin-dir` / `--plugin-url` | trying it without installing | `claude --plugin-dir ./plugins/jira-assets` |

To publish to the public Claude community marketplace, run
`claude plugin validate ./plugins/jira-assets` and submit at
[platform.claude.com/plugins/submit](https://platform.claude.com/plugins/submit).

## Before you push this anywhere

- `.env` holds your API token and is gitignored — confirm with `git ls-files | grep env`
  that only `.env.example` is tracked.
- Anyone you share with sees your Jira site only if they set it themselves; the
  committed `.env.example` ships a placeholder.
- A token grants everything your Jira account can do. Share the repo, never the token.
