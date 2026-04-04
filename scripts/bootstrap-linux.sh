#!/usr/bin/env bash
set -euo pipefail

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "This helper currently targets Linux only."
  exit 1
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CCBOT_DIR="${CCBOT_DIR:-$HOME/.ccbot}"
ENV_PATH="${CCBOT_DIR}/.env"
BIN_DIR="${CCBOT_DIR}/bin"
LOG_DIR="${CCBOT_DIR}/logs"
SYSTEMD_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
SERVICE_NAME="${CCBOT_SYSTEMD_SERVICE_NAME:-io.github.telegramcodexccbot.service}"
SERVICE_PATH="${SYSTEMD_DIR}/${SERVICE_NAME}"
LAUNCHER_PATH="${BIN_DIR}/ccbot-launch"
PATH_VALUE="/usr/local/bin:/usr/bin:/bin:${HOME}/.local/bin"

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1"
    exit 1
  fi
}

require_cmd uv
require_cmd tmux
require_cmd codex

mkdir -p "$BIN_DIR" "$LOG_DIR" "$SYSTEMD_DIR"

if [[ ! -f "$ENV_PATH" ]]; then
  cp "$REPO_DIR/.env.example" "$ENV_PATH"
  echo "Created $ENV_PATH from .env.example"
else
  echo "Keeping existing $ENV_PATH"
fi

cat >"$LAUNCHER_PATH" <<EOF
#!/usr/bin/env bash
export PATH="$PATH_VALUE"
export HOME="$HOME"
cd "$REPO_DIR"
exec /usr/bin/env uv run ccbot "\$@"
EOF
chmod +x "$LAUNCHER_PATH"

cat >"$SERVICE_PATH" <<EOF
[Unit]
Description=TelegramCodexCCBot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$CCBOT_DIR
ExecStart=$LAUNCHER_PATH
Restart=always
RestartSec=3
Environment=PATH=$PATH_VALUE
Environment=HOME=$HOME
StandardOutput=append:$LOG_DIR/ccbot.out.log
StandardError=append:$LOG_DIR/ccbot.err.log

[Install]
WantedBy=default.target
EOF

cd "$REPO_DIR"
uv sync
uv run ccbot hook --install

token_line="$(grep -E '^TELEGRAM_BOT_TOKEN=' "$ENV_PATH" || true)"
user_line="$(grep -E '^ALLOWED_USERS=' "$ENV_PATH" || true)"
token_ready=1
user_ready=1
started="no"

if [[ -z "$token_line" || "$token_line" == "TELEGRAM_BOT_TOKEN=your_bot_token_here" ]]; then
  token_ready=0
fi

if [[ -z "$user_line" || "$user_line" == "ALLOWED_USERS=123456789,987654321" ]]; then
  user_ready=0
fi

if command -v systemctl >/dev/null 2>&1; then
  if [[ "$token_ready" -eq 1 && "$user_ready" -eq 1 ]]; then
    systemctl --user daemon-reload
    systemctl --user enable --now "$SERVICE_NAME"
    started="yes"
  fi
else
  echo "systemctl not found; service file was created but not started."
fi

cat <<EOF

Bootstrap complete.

Paths:
  env:        $ENV_PATH
  launcher:   $LAUNCHER_PATH
  service:    $SERVICE_PATH

Next steps:
  1. Edit $ENV_PATH
     - TELEGRAM_BOT_TOKEN
     - ALLOWED_USERS
     - optional OPENAI_API_KEY / OPENAI_BASE_URL
  2. Run: codex login
  3. Optional multi-account:
     ~/.ccbot/bin/codex-account save main
     ~/.ccbot/bin/codex-account save backup
     ~/.ccbot/bin/codex-account use main

Service started automatically: $started
EOF

if [[ "$started" == "no" ]]; then
  cat <<EOF

If the service is not running yet, use:
  systemctl --user daemon-reload
  systemctl --user enable --now "$SERVICE_NAME"

If you want the user service to survive reboot on a VPS, run once:
  sudo loginctl enable-linger "$USER"
EOF
fi
