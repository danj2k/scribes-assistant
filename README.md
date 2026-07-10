# Scribe's Assistant

A Discord bot for automated transcription of tabletop RPG sessions. Records voice from Discord voice channels, transcribes speech using a local Whisper model, and delivers timestamped transcripts with speaker identification.

## Quick Start (Server Admin)

### Prerequisites

The setup involves two roles — the **bot administrator** (you) who runs the infrastructure, and the **server administrator / DM** who controls access to the Discord server.

#### Bot administrator (you)

- Docker and Docker Compose installed on the server
- A Discord bot application created at https://discord.com/developers/applications

##### Creating a Bot Application

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications), click **New Application**, and give it a name (e.g. "Scribe's Assistant").
2. Navigate to the **Bot** tab and click **Add Bot**.
3. Under **Privileged Gateway Intents**, enable **Message Content Intent** and **Server Members Intent** (these are not strictly required for this bot but may be useful for future features).
4. **Privacy** — in the **Bot** tab, ensure **Public Bot** is **disabled** (this is the default). With this disabled, your bot will not appear in any public bot directory. The only way someone can add it to their server is by having the direct invite link that you will generate in the next step. This prevents unauthorised servers from adding your bot.
5. Copy the **Bot Token** — you will need this during setup (see below).

#### Server administrator / DM

The bot needs to be invited to your Discord server with the correct permissions. This step requires the **Manage Server** permission on the target Discord server.

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications), select your bot application, and navigate to **OAuth2** > **URL Generator**.
2. Under **Scopes**, select `bot` **and** `applications.commands`.
   - `bot` — grants the bot access to your server.
   - `applications.commands` — allows the bot to register and use slash commands. Without this scope, none of the bot's commands will appear in Discord.
3. Under **Bot Permissions**, select the following:
   - Send Messages
   - Create Public Threads
   - Attach Files
   - Use Voice Activity
4. Copy the generated URL at the bottom of the page and open it in your browser.
5. Select the server you want to add the bot to and authorise it.

For more details, see Discord's guide on [Adding a Bot to a Server](https://discord.com/developers/docs/getting-started#step-2-adding-your-bot-to-servers).

> **Note:** If you are not the server administrator, send the generated invite link to your DM or server admin and ask them to complete this step.

### Setup

1. **Clone the repository:**
   ```bash
   git clone https://github.com/<your-org>/scribes-assistant.git
   cd scribes-assistant
   ```

2. **Store your Discord bot token securely** (Docker secrets):
   ```bash
   mkdir -p secrets
   echo "YOUR_BOT_TOKEN_HERE" > secrets/bot_token
   chmod 600 secrets/bot_token
   ```
   The bot service receives this token via a read-only Docker secret mounted at `/run/secrets/bot_token`. It is never baked into an image or visible in `docker inspect`.

3. **Review and edit `config.yaml`:**
   ```yaml
   transcriber:
     model: small                # whisper tiny/base/small/medium/large-v3
     threads: 2                  # CPU cores for transcription (leave headroom for bot)
   lexicon:
     enabled: true
   ```

4. **Start the services:**
   ```bash
   docker compose up -d
   ```

5. **Download the model (first run only):**
   ```bash
   docker compose exec transcriber python scripts/download_model.py
   ```
   This downloads the Whisper small model (~609MB) to the shared volume. The script:
   - Skips download if model files already exist
   - Shows download progress (MB and percentage)
   - Verifies all required files are extracted
   - Cleans up the archive after extraction

   To force re-download or use a different model size:
   ```bash
   docker compose exec transcriber python scripts/download_model.py --force
   docker compose exec transcriber python scripts/download_model.py --model tiny
   ```

   The transcriber will also attempt to download the model automatically on first start if it detects missing files.

### Stopping

```bash
docker compose down
```

Audio recordings and transcripts persist in the Docker volume across restarts.

---

## User Guide

### Getting Started

Once the bot is set up on your Discord server, join a voice channel and use the slash commands below. The bot needs to be in the same server (but not necessarily in the voice channel — it will join automatically when you start a session).

### Commands

| Command | Description |
|---------|-------------|
| `/start` | Start recording. The bot joins your voice channel and begins capturing audio. |
| `/stop` | Stop recording. The bot leaves the voice channel and queues the session for transcription. Also triggers automatically if the bot is left alone in the voice channel. |
| `/status` | Check the current session status and recording duration. |
| `/session` | List previous sessions and their transcript status. |
| `/invite` | Get a link to invite the bot to another server (if permitted). |
| `/help` | Show available commands and usage information. |
| `/lexicon add term:<word> description:<description>` | Add a word to the transcription lexicon. |
| `/lexicon list` | Show all words currently in the lexicon. |
| `/lexicon remove term:<word>` | Remove a word from the lexicon. |

### How It Works

1. **Start a session:** Join a voice channel and type `/start`. The bot joins and begins recording.
2. **Play your game:** The bot records in the background. You don't need to do anything.
3. **End the session:** Type `/stop`, or simply disconnect from the voice channel. If the bot is the only one left, it will automatically end the session after a short idle period, save the recording, and disconnect. Either way, the audio is queued for transcription.
4. **Wait for the transcript:** Transcription takes a few minutes (depending on session length). The bot creates a thread in the channel where you issued the command and posts the transcript there, keeping each session's output organised.
5. **Check status:** Use `/status` to see if transcription is in progress or complete.

### Lexicon

The lexicon helps the transcription model recognise unfamiliar or fantasy words. For example, if your campaign features a character named "Tharion" or a spell called "Zephyr's Grasp", adding these to the lexicon improves transcription accuracy.

Slash commands use Discord's named parameters, so there's no ambiguity between the word and its description:

**Examples:**
- `/lexicon add term:Tharion description:Character name, wizard NPC`
- `/lexicon add term:Zephyr's Grasp description:Spell name, used by the party druid`
- `/lexicon add term:Waterdeep description:City name, major location in the campaign`

Words are used in two ways to improve transcription:
1. **Hotwords bias** — lexicon terms are passed to the Whisper model as decoding hints, strongly biasing it to recognise custom vocabulary during transcription.
2. **Post-correction** — after transcription, the bot applies fuzzy matching to correct common misrecognitions of lexicon words.

---

## Architecture

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for full system documentation.

**Two containers:**
- `bot` — lightweight, handles Discord interaction and voice capture
- `transcriber` — heavyweight, runs sherpa-onnx speech-to-text

**Shared volume** at `/data` stores recordings, transcripts, and model weights.

**SQLite database** at `/data/queue.db` coordinates session state between containers.

## Configuration

Settings are defined in `config.yaml` (see `config.yaml.example` for all options with documented defaults).

| Setting | Default | Description |
|---------|---------|-------------|
| `discord.guild_id` | `""` (empty) | Numeric [Snowflake ID](https://discord.com/developers/docs/reference#snowflakes) of the server to restrict the bot to (empty = any server). See [Obtaining your Server ID](#obtaining-your-server-id). |
| `discord.permissions.restrict_commands` | `false` | Restrict command usage to specific roles |
| `discord.permissions.allowed_roles` | `[]` | Roles permitted when restrictions are enabled |
| `lexicon.enabled` | `true` | Enable/disable lexicon features |
| `lexicon.fuzzy_threshold` | `0.2` | Levenshtein distance for fuzzy post-correction matching |
| `session.idle_timeout` | `60` | Seconds before auto-ending session when bot is alone |
| `transcriber.model` | `small` | Whisper model size (tiny/base/small/medium/large-v3) |
| `transcriber.threads` | `0` | CPU threads for transcription (0 = all available) |
| `transcriber.poll_interval` | `10` | Seconds between queue polling cycles |
| `logging.level` | `INFO` | Log level (DEBUG/INFO/WARNING/ERROR) |
| `logging.max_size_mb` | `10` | Max log file size before rotation |
| `logging.backup_count` | `5` | Number of rotated log files to keep |

### Obtaining your Server ID

Discord uses numeric [Snowflake IDs](https://discord.com/developers/docs/reference#snowflakes) (17–20 digits) for servers, channels, users, and other resources — not names. To copy your server's ID:

1. Enable **Developer Mode** in Discord (User Settings > Advanced > Developer Mode).
2. Right-click your server's name in the sidebar and select **Copy Server ID**.


## Secrets

Secrets are mounted as files via Docker secrets:

| Secret | File | Description |
|--------|------|-------------|
| `bot_token` | `/run/secrets/bot_token` | Discord bot token |

## Logging

The bot logs to local files for debugging infrastructure issues that cannot be reported through Discord. Log files are stored in the shared Docker volume and persist across container restarts.

| Container | Log file | What it contains |
|-----------|----------|------------------|
| `bot` | `/data/logs/bot.log` | Discord connection events, voice channel joins/leaves, slash command invocations, session lifecycle |
| `transcriber` | `/data/logs/transcriber.log` | Transcription jobs started/completed, model loading, ffmpeg conversions, errors |

### Viewing logs

```bash
# Tail the bot log
docker compose exec bot tail -f /data/logs/bot.log

# Tail the transcriber log
docker compose exec transcriber tail -f /data/logs/transcriber.log

# View recent errors
docker compose exec bot grep ERROR /data/logs/bot.log | tail -20
```

### Log levels

- **INFO** — normal operations (session started, transcription complete)
- **WARNING** — recoverable issues (reconnection attempt, transcription took longer than expected)
- **ERROR** — failures requiring attention (transcription failed, model not found, database locked)

Logs rotate automatically — older log files are archived when they exceed 10MB.

## License

See [LICENSE](LICENSE).
