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
2. Under **Scopes**, select `bot`.
3. Under **Bot Permissions**, select the following:
   - Send Messages
   - Create Public Threads
   - Attach Files
   - Use Voice Activity
   - Application Commands
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

2. **Create the bot token secret:**
   ```bash
   mkdir -p secrets
   echo "YOUR_BOT_TOKEN_HERE" > secrets/bot_token
   chmod 600 secrets/bot_token
   ```

3. **Review and edit `config.yaml`:**
   ```yaml
   whisper_model: small          # whisper tiny/base/small/medium/large
   transcriber_threads: 2        # CPU cores for transcription (leave headroom for bot)
   transcript_channel: null      # null = channel where commands are issued
   lexicon_enabled: true
   ```

4. **Start the services:**
   ```bash
   docker compose up -d
   ```

5. **Download the model (first run only):**
   ```bash
   docker compose exec transcriber python download_model.py
   ```
   This downloads ~500MB of model weights to the shared volume. Subsequent container rebuilds will not re-download.

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
4. **Wait for the transcript:** Transcription takes a few minutes (depending on session length). The bot will post the transcript to the channel where you issued the command.
5. **Check status:** Use `/status` to see if transcription is in progress or complete.

### Lexicon

The lexicon helps the transcription model recognise unfamiliar or fantasy words. For example, if your campaign features a character named "Tharion" or a spell called "Zephyr's Grasp", adding these to the lexicon improves transcription accuracy.

Slash commands use Discord's named parameters, so there's no ambiguity between the word and its description:

**Examples:**
- `/lexicon add term:Tharion description:Character name, wizard NPC`
- `/lexicon add term:Zephyr's Grasp description:Spell name, used by the party druid`
- `/lexicon add term:Waterdeep description:City name, major location in the campaign`

Words are used as hints during transcription — the model will be more likely to recognise them correctly.

---

## Architecture

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for full system documentation.

**Two containers:**
- `bot` — lightweight, handles Discord interaction and voice capture
- `transcriber` — heavyweight, runs sherpa-onnx speech-to-text

**Shared volume** at `/data` stores recordings, transcripts, and model weights.

**SQLite database** at `/data/queue.db` coordinates session state between containers.

## Configuration

| Setting | Default | Description |
|---------|---------|-------------|
| `whisper_model` | `small` | Whisper model size (tiny/base/small/medium/large) |
| `transcriber_threads` | `2` | CPU cores allocated to transcription |
| `transcript_channel` | `null` | Channel for transcript delivery (null = command channel) |
| `lexicon_enabled` | `true` | Enable/disable lexicon features |

## Secrets

Secrets are mounted as files via Docker secrets:

| Secret | File | Description |
|--------|------|-------------|
| `bot_token` | `/run/secrets/bot_token` | Discord bot token |

## License

See [LICENSE](LICENSE).
