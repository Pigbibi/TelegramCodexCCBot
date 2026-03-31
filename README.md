# TelegramCodexCCBot

[中文文档](README_CN.md)

> A Codex-focused fork of the original CCBot.
> The CLI/package name stays `ccbot`.

**Upstream project:** https://github.com/six-ddc/ccbot  
**License:** MIT (kept from upstream)

Control Codex sessions remotely through Telegram while keeping tmux as the source of truth. This lets you monitor, answer, interrupt, resume, and clean up real terminal sessions from your phone without switching to a separate SDK session.

https://github.com/user-attachments/assets/15ffb38e-5eb9-4720-93b9-412e4961dc93

## Why this fork exists

The upstream project is a solid base, but this fork is tuned for a Codex workflow:

- `codex` is the default command for new tmux windows
- transcript parsing and monitoring target modern Codex JSONL output under `~/.codex`
- Telegram delivery and topic isolation were hardened for long-running Codex sessions
- the fork keeps tmux-first ergonomics, so you can always return to the same terminal session on desktop

In short: this is still CCBot, but adapted to how Codex is actually used day to day.

## What changed compared with upstream?

Compared with https://github.com/six-ddc/ccbot, this fork adds or changes the following:

- **Codex-first defaults** — uses `codex` instead of the upstream legacy defaults
- **Recursive Codex transcript discovery** — monitors modern Codex session logs under `~/.codex`
- **Better Telegram delivery** — improved polling behavior, commentary forwarding, and notification handling
- **Safer topic/session isolation** — prevents multiple Telegram topics from attaching to the same live session
- **Stale state cleanup** — removes dead topic bindings, stale `session_map` entries, and stale window state
- **Cleaner topic shutdown** — `/kill` and topic deletion flows clean up tmux/session state more reliably
- **Account rotation support** — supports isolated `CODEX_HOME` homes plus saved account snapshots for manual switching and usage-limit failover
- **Codex-oriented documentation** — examples, setup, and command descriptions now assume a Codex workflow

## Features

- **Topic-based sessions** — each Telegram topic maps 1:1 to a tmux window and Codex session
- **Real-time notifications** — assistant replies, thinking, tool calls, tool results, and local command output can be forwarded to Telegram
- **Interactive UI support** — navigate AskUserQuestion, ExitPlanMode, and permission prompts from inline keyboards
- **Voice message transcription** — voice messages can be transcribed with OpenAI and forwarded as text
- **Resume existing sessions** — choose an existing Codex session in a directory and continue from there
- **Topic cleanup** — stale topics, stale tmux windows, and dead bindings are cleaned up more safely
- **Usage-limit failover** — when a session hits `usage_limit_exceeded`, the next message can rotate to another saved account in a fresh session
- **Persistent state** — thread bindings, display names, offsets, and monitor state survive restarts

## Prerequisites

- **tmux** installed and available in PATH
- **Codex CLI** installed and working locally
- A **Telegram bot** with threaded/forum mode enabled

## Installation

### Option 1: install from GitHub

```bash
# with uv
uv tool install git+https://github.com/Pigbibi/TelegramCodexCCBot.git

# or with pipx
pipx install git+https://github.com/Pigbibi/TelegramCodexCCBot.git
```

### Option 2: install from source

```bash
git clone https://github.com/Pigbibi/TelegramCodexCCBot.git
cd TelegramCodexCCBot
uv sync
```

## Configuration

### 1. Create a Telegram bot

1. Talk to [@BotFather](https://t.me/BotFather)
2. Create a bot and get the token
3. Open the bot settings mini app
4. Enable **Threaded Mode**

### 2. Create `~/.ccbot/.env`

```ini
TELEGRAM_BOT_TOKEN=your_bot_token_here
ALLOWED_USERS=your_telegram_user_id
CCBOT_CODEX_COMMAND=codex
CCBOT_SHOW_COMMENTARY_MESSAGES=true
```

### Required variables

| Variable | Description |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather |
| `ALLOWED_USERS` | Comma-separated Telegram user IDs allowed to control the bot |

### Common optional variables

| Variable | Default | Description |
| --- | --- | --- |
| `CCBOT_DIR` | `~/.ccbot` | Config and state directory |
| `TMUX_SESSION_NAME` | `ccbot` | tmux session name used by the bot |
| `CCBOT_CODEX_COMMAND` | `codex` | Command used when creating a new window |
| `CCBOT_CODEX_PROJECTS_PATH` | `~/.codex` | Transcript root to scan |
| `MONITOR_POLL_INTERVAL` | `2.0` | Poll interval in seconds |
| `CCBOT_SHOW_COMMENTARY_MESSAGES` | `false` | Forward Codex commentary/thinking messages |
| `CCBOT_SHOW_HIDDEN_DIRS` | `false` | Show dot-directories in the directory picker |
| `OPENAI_API_KEY` | _(none)_ | Used for voice transcription |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | Custom OpenAI-compatible endpoint |

Telegram formatting uses MarkdownV2 with plain-text fallback when needed.

### Non-interactive servers / VPS

If Codex runs on a server where you do not want approval prompts in the terminal UI:

```ini
CCBOT_CODEX_COMMAND=IS_SANDBOX=1 codex --dangerously-bypass-approvals-and-sandbox
```

## Multi-account switching and failover

This fork supports isolated account homes for Codex under `~/.ccbot/accounts/homes/`.

Typical flow:

```bash
# login account A
codex login
~/.ccbot/bin/codex-account save main

# login account B
codex login
~/.ccbot/bin/codex-account save backup

# choose the default account for newly created sessions
~/.ccbot/bin/codex-account use main
```

When a live session emits `usage_limit_exceeded`, CCBot marks that window as exhausted. On the next message, it can create a fresh tmux window on the next saved account and forward the message there.

Important: this is **session rotation**, not seamless continuation of the exact same Codex session.

## Session tracking

By default, this fork scans Codex transcript files under `~/.codex`.

If you want automatic session-to-window tracking via the CLI hook, install it with:

```bash
ccbot hook --install
```

This command enables Codex hooks in `~/.codex/config.toml` and writes a
`SessionStart` hook to `~/.codex/hooks.json`.

Manual equivalent:

`~/.codex/config.toml`

```toml
[features]
codex_hooks = true
```

`~/.codex/hooks.json`

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup|resume",
        "hooks": [
          {
            "type": "command",
            "command": "ccbot hook",
            "statusMessage": "Registering CCBot session",
            "timeout": 5
          }
        ]
      }
    ]
  }
}
```

The hook writes window/session mappings into `$CCBOT_DIR/session_map.json`, which helps the bot keep tmux windows associated with Codex sessions even after clears or restarts.

## Usage

```bash
# installed tool
ccbot

# from source
uv run ccbot
```

### Bot commands

| Command | Description |
| --- | --- |
| `/start` | Show the welcome message |
| `/history` | Show message history for the current topic |
| `/screenshot` | Capture the current terminal pane |
| `/esc` | Send Escape to Codex |
| `/kill` | Kill the bound tmux window and clean up the topic binding |
| `/unbind` | Unbind the topic without killing the running tmux window |
| `/usage` | Open Codex usage info in the TUI and send the parsed result |

### Forwarded Codex slash commands

| Command | Description |
| --- | --- |
| `/clear` | Clear conversation history |
| `/compact` | Compact context |
| `/cost` | Show token/cost usage |
| `/help` | Show Codex help |
| `/memory` | Edit AGENTS.md |
| `/model` | Switch the model |

Other unknown slash commands are forwarded to Codex as-is.

## Topic workflow

**1 topic = 1 tmux window = 1 active session.**

### Start a session from Telegram

1. Create a new Telegram topic
2. Send any message
3. Pick a directory from the browser
4. Resume an existing session or create a new one
5. CCBot creates a tmux window and forwards your pending message

### Continue working

After a topic is bound, just keep sending text or voice messages in that topic.

### Stop working

- close/delete the Telegram topic, or
- use `/kill`, or
- use `/unbind` if you want to keep the tmux window alive but detach the topic

## Notifications

The monitor polls transcript files and can forward:

- assistant replies
- commentary / thinking output
- tool use and tool results
- local command output
- usage-limit exhaustion events

## Running Codex manually in tmux

```bash
tmux attach -t ccbot
tmux new-window -n myproject -c ~/Code/myproject
codex
```

The window must live inside the configured `ccbot` tmux session.

## Data storage

| Path | Description |
| --- | --- |
| `$CCBOT_DIR/state.json` | Thread bindings, window state, display names, offsets |
| `$CCBOT_DIR/session_map.json` | Hook-generated tmux window ↔ session mappings |
| `$CCBOT_DIR/monitor_state.json` | Monitor byte offsets per session |
| `$CCBOT_DIR/pending_topic_deletions.json` | Deferred topic deletions after local cleanup |
| `~/.codex/` | Codex transcript root (read-only) |
| `~/.ccbot/accounts/` | Optional saved account homes and snapshots |

## File structure

```text
src/ccbot/
├── __init__.py
├── account_manager.py
├── bot.py
├── config.py
├── hook.py
├── main.py
├── markdown_v2.py
├── monitor_state.py
├── screenshot.py
├── session.py
├── session_monitor.py
├── terminal_parser.py
├── tmux_manager.py
├── transcribe.py
├── transcript_parser.py
├── utils.py
└── handlers/
```

## Upstream and license

This project is based on the original work by **six-ddc**:

- Upstream repository: https://github.com/six-ddc/ccbot
- License: MIT

The upstream MIT license is preserved in this fork, and the fork-specific modifications are additionally copyrighted by Pigbibi.
