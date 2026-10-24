# CodexPulse

A self-hosted macOS Discord bot for monitoring Codex usage, forecasting limits,
and resuming tasks after a usage-limit stop.

- Tracks five-hour and weekly limits, credits, and estimated time to exhaustion.
- Updates one persistent Usage Monitor and posts temporary event notifications.
- Analyzes local model, reasoning-effort, and project usage telemetry.
- Monitors CPU, memory, battery, and recent Computer Use activity.
- Stores history in SQLite and the Discord token in macOS Keychain.

This is an unofficial personal-use tool, unaffiliated with OpenAI or Discord.
It depends on the local Codex CLI app-server API and session formats, which may
change between app versions.

## Requirements

- macOS and Python 3.13 or 3.14.
- [uv](https://docs.astral.sh/uv/) for dependency management.
- An installed, signed-in Codex CLI with `app-server` and `queue` support.
- A Discord server where you can install a bot.

## Install and configure

```sh
git clone https://github.com/n3dypop/CodexPulse.git
cd CodexPulse
uv sync --locked --python 3.14
mkdir -p "$HOME/Library/Application Support/CodexPulse"
cp -n config.example.toml "$HOME/Library/Application Support/CodexPulse/config.toml"
chmod 600 "$HOME/Library/Application Support/CodexPulse/config.toml"
```

Edit that configuration file before continuing:

1. Set `codex.binary` to the absolute path of your Codex CLI executable. The
   example points to the CLI bundled with the macOS ChatGPT app; check your own
   installation path.
2. Create an application and bot in the
   [Discord Developer Portal](https://discord.com/developers/applications).
3. Replace the zero placeholders for `application_id`, `guild_id`, `channel_id`,
   and `owner_user_id` in `[discord]`. `mention_user_id = 0` uses the owner.
4. Review `monitor.timezone` and set `codex.auto_resume = false` if you only want
   monitoring. The example enables automatic resume.

Keep configuration and runtime data outside the checkout. The default configuration
path is `~/Library/Application Support/CodexPulse/config.toml`; relative database
paths are resolved against its directory. Use `codex-pulse --config /path/to/config.toml`
before a subcommand to select another configuration.

Store the token, generate the invite link, then start the bot:

```sh
uv run --locked codex-pulse set-token
uv run --locked codex-pulse invite-url
# Open the printed URL to install the bot, then:
uv run --locked codex-pulse run
```

The bot needs **View Channel**, **Send Messages**, **Embed Links**, **Attach Files**,
and **Read Message History** in the configured channel. Add **Pin Messages** only
if `pin_status_message = true`. Privileged Gateway Intents are unnecessary.
The `DISCORD_BOT_TOKEN` environment variable can override Keychain for foreground
runs; keep real tokens out of configuration files and shell history.

## Use

The Usage Monitor updates in place. Important events use separate temporary
messages with owner mentions. Slash commands are restricted to `owner_user_id`
and use private responses.

| Command | Purpose |
| --- | --- |
| `/status`, `/forecast`, `/history` | View usage and forecasts |
| `/refresh`, `/notify`, `/test-notification` | Refresh or control event notifications |
| `/model-usage`, `/project-usage`, `/why` | Inspect attributed usage |
| `/resume-queue`, `/resume-next`, `/resume-select` | Inspect or explicitly resume queued tasks |
| `/health`, `/computer`, `/screenshot` | Check service and system state |
| `/help` | Show the complete command guide |

`/screenshot` uploads all active displays to Discord in a temporary private
response. It requires macOS Screen & System Audio Recording permission for the
executing process. Grant it in System Settings, then restart the bot.

With the default `auto_detect_thread = true`, automatic resume selects the most
recent task whose latest turn stopped with a usage-limit error, within the
configured lookback. Additional tasks require an explicit command. Resume is
driven by five-hour reset detection; weekly-only reset resume is unsupported.
Changing `auto_detect_thread` to `false` uses the explicitly configured `codex.thread`
without the automatic usage-error filter. Run only one instance with automatic
resume enabled. The bot does not change model or reasoning settings.

`uv run --locked codex-pulse collect-once` runs a full collection cycle and prints
JSON. It writes local history and **can resume work when automatic resume is
enabled**; it does not post Discord notifications.

## Run at login

Stop any foreground instance with Control-C, then run:

```sh
uv run --locked codex-pulse install-launchd
```

This starts a LaunchAgent that runs at login and restarts after failures. Keep
the checkout and its `.venv` at the same location. Logs go to
`~/Library/Logs/CodexPulse.log`. Monitoring pauses while the Mac sleeps or is
logged out; it refreshes after wake.

## Data and limitations

Forecasts are statistical estimates. Sparse observations, concurrent tasks,
model changes, and app updates can reduce accuracy. Five-hour usage increments
are attributed to turns using a token-weight estimate, rather than measured
per-task billing.

The bot reads local session files without modifying them. It imports numeric
telemetry and session metadata, including local paths and task identifiers, into
SQLite. Discord receives usage summaries, model and project names, resume task
labels, system metrics, and explicitly requested screenshots. Session message
bodies are not forwarded. Restrict access to the monitoring channel.

History retention defaults to 90 days; resumed queue entries default to 30 days.
Logs rotate at 5 MB with three backups. Deleting a Discord response does not
provide a guarantee about Discord's retention of uploaded files.

## Update, build, and uninstall

Run updates from the checkout after stopping any foreground instance:

```sh
git pull --ff-only
uv sync --locked --python 3.14
uv run --locked codex-pulse install-launchd
```

Build a wheel and source archive with `uv build`. Both include the MIT license;
third-party dependencies are installed separately by the package manager.

To stop and remove the LaunchAgent:

```sh
uv run --locked codex-pulse uninstall-launchd
# Optional: remove the stored token.
uv run --locked codex-pulse delete-token
```

Uninstalling the agent preserves configuration, history, and logs. Remove those
files manually if desired, after reviewing their contents.

## License

[MIT](LICENSE). OpenAI, Codex, and Discord names and trademarks belong to their
respective owners. No third-party logo artwork is distributed.
