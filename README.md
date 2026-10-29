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
/plugin marketplace add azadhash/asst_tool
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

