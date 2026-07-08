# Scribe's Assistant — D&D Discord Session Transcription Bot — Design Document

```yaml
doc_type: design_document
version: 0.1.0
status: draft
audience: [human_developer, ai_coding_agent]
project_name: scribes-assistant
display_name: "Scribe's Assistant"
last_updated: 2026-07-06
```

> **Note for AI agents processing this document:** Sections are numbered and self-contained where possible. Each major section that implies buildable work includes a `### Implementation Notes` subsection with concrete, actionable detail (libraries, file layout, config keys, pseudocode). Treat `### Open Questions` subsections as items to raise with the human, not to silently decide.

---

## 1. Project Summary

| Field | Value |
|---|---|
| Goal | Record and transcribe Dungeons & Dragons sessions played over a Discord voice channel, producing a readable, speaker-attributed transcript. |
| Latency requirement | None — batch/offline processing is acceptable and preferred. |
| Deployment target | Home Ubuntu 26.04 box — 16GB RAM, 4 cores (Intel i3-8100), Intel UHD Graphics 630 (Coffee Lake) |
| Resource constraint | CPU-only inference should be assumed. The UHD 630 iGPU has no practical ML acceleration path worth building around (no CUDA, and OpenVINO/oneAPI support for this generation is marginal for the models in question) — treat this as a CPU-only deployment for planning purposes. With 16GB RAM, memory is a much more comfortable constraint than on a small VPS, but the 4-core i3-8100 means wall-clock transcription time (not memory) is now the main thing to keep an eye on for a ~3.25 hour session. |
| Max session length | ~3.25 hours |
| Cloud usage | **None for audio/transcription.** All speech-to-text must run locally. Cloud APIs are acceptable only for non-audio, opt-in features (e.g. optional LLM cleanup of the final text transcript, if the user explicitly enables it later). |
| Language | Python 3.11+ |
| Packaging | Docker container(s), managed via Docker Compose |
| Excluded | Node.js-based Discord voice libraries (e.g. discord.js + discord-recorder ecosystems) — Python-first stack preferred |

---

## 2. High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         Discord Voice Channel                    │
└───────────────────────────────┬─────────────────────────────────┘
                                 │  Opus audio (per-user SSRC streams)
                                 ▼
                     ┌───────────────────────┐
                     │   Bot / Recorder svc   │  (py-cord, discord.py fork with voice recv)
                     │  - joins VC on command │
                     │  - captures per-user   │
                     │    PCM streams         │
                     │  - writes raw audio    │
                     │    segments to disk    │
                     └───────────┬────────────┘
                                 │  per-speaker audio files (WAV, 16kHz mono)
                                 ▼
                     ┌───────────────────────┐
                     │   Session Store        │  filesystem + SQLite metadata
                     │  /data/sessions/<id>/  │
                     └───────────┬────────────┘
                                 │  triggered after session ends (or on demand)
                                 ▼
                     ┌───────────────────────┐
                     │   Transcription Worker  │  loud.cpp (whisper.cpp + CPU diarization)
                     │  - runs per audio      │
                     │    segment, sequential │
                     │  - low-memory model    │
                     │    size, streamed      │
                     ├───────────────────────┤
                     │  Lexicon / Prompt      │  campaign glossary injected as
                     │  Injection             │  Whisper "initial_prompt"
                     ├───────────────────────┤
                     │  Speaker Attribution   │  Discord user ID -> display name,
                     │                        │  with multi-speaker-per-user
                     │                        │  override table (per session)
                     └───────────┬────────────┘
                                 │  merged, time-ordered transcript segments
                                 ▼
                     ┌───────────────────────┐
                     │  Post-Processing       │  merge, dedupe silence, optional
                     │                        │  local text cleanup pass
                     └───────────┬────────────┘
                                 │
                                 ▼
                     ┌───────────────────────┐
                     │  Output                │  .md / .txt / .srt transcript,
                     │                        │  delivered via Discord DM/channel
                     │                        │  upload or written to /data/output
                     └───────────────────────┘
```

### 2.1 Why capture per-speaker instead of a single mixed stream

Discord's voice receive API delivers **separate audio streams per connected user** (identified by SSRC). This is a major advantage over recording a single mixed-down channel: it gives free, perfect speaker separation for anyone using their own Discord account, with no diarization ML needed for that case. Diarization (guessing who is speaking from audio characteristics alone) is only needed for the "two people sharing one webcam/mic" case — see Section 5.

### 2.2 Components as separate processes/containers

Recommended split into two Docker services sharing a volume:

1. **`bot` service** — always-on, lightweight. Handles Discord gateway connection, voice capture, commands. Low memory footprint (no ML models loaded).
2. **`transcriber` service** — invoked/queued after a session ends (or manually via a command), loads the Whisper model, does the heavy lifting, then can idle or exit. Keeping this separate means the memory-hungry ML process doesn't need to stay resident 24/7, and the bot stays responsive even during long transcription runs.

Communication between them can be as simple as a job file/flag in the shared volume plus a lightweight file-watcher, or a tiny local queue (SQLite table used as a job queue, or Redis if you don't mind one more small container). Given the low-resource constraint, **recommend the SQLite-as-queue approach** to avoid an extra Redis container.

### Implementation Notes
- Repo layout:
  ```
  scribes-assistant/
  ├── docker-compose.yml
  ├── bot/
  │   ├── Dockerfile
  │   ├── main.py
  │   ├── cogs/
  │   │   ├── recording.py
  │   │   └── session_admin.py
  │   └── requirements.txt
  ├── transcriber/
  │   ├── Dockerfile           # builds/vendors the loud.cpp binary, see note below
  │   ├── worker.py
  │   ├── lexicon.py
  │   └── requirements.txt     # Python glue only (subprocess, DB access) — no ML packages needed
  ├── shared/
  │   ├── models.py          # SQLAlchemy models: Session, AudioSegment, SpeakerMap, LexiconEntry
  │   └── db.py
  ├── data/                   # bind-mounted volume
  │   ├── sessions/<session_id>/raw/<discord_user_id>_<ssrc>_<timestamp>.opus
  │   ├── output/<session_id>.md
  │   └── db.sqlite3
  └── config/
      ├── settings.yaml
      └── lexicon.yaml         # per-campaign lexicon, human-editable
  ```
- `loud.cpp` ships as a compiled binary (with prebuilt releases per Section 6.2) rather than a Python/pip package — the `transcriber` Dockerfile should either download the appropriate Linux release binary or build it from source in a build stage, then copy the binary into the final image alongside a slim Python runtime for the orchestration glue (`worker.py`). This keeps the transcriber image free of PyTorch/CTranslate2 entirely.

### Resolved
- Deployment is the Ubuntu 26.04 box, not a VPS. It does have an Intel UHD 630 iGPU, but this offers no special acceleration value for this project (no CUDA, no worthwhile OpenVINO path for the models in use) — treated as CPU-only throughout this document (see Section 1).
- Max expected session length: ~3.25 hours (see Section 1).

---

## 3. Discord Voice Capture

### 3.1 Library choice

`discord.py` mainstream branch does **not** support voice *receiving*, only sending. For receiving per-user audio, use one of:

- **`py-cord`** (a discord.py fork) — has built-in voice recording (`VoiceClient.start_recording`), actively maintained, supports per-user sinks. **Recommended default.**
- **`discord.py` + a voice-recv extension** (e.g. `discord-ext-voice-recv`) — works with discord.py directly if you have a preference for staying on mainline discord.py.

Either gives you an Opus decoder callback per SSRC/user, from which you can write PCM to per-user WAV files.

### 3.2 Capture flow

1. User runs `/record start` in a text channel while in a voice channel.
2. Bot joins the VC, opens a recording session (creates a `Session` row, a session folder).
3. As audio arrives per user, it's buffered and flushed periodically (e.g. every 30–60s) to segment WAV files rather than one giant file per user — this bounds memory use and gives crash resilience (a crash loses at most one flush interval, not the whole session).
4. `/record stop` (or automatic stop on the bot being the last one left in the channel, with a grace period) finalizes segments and marks the session ready for transcription.
5. A queue entry is created for the transcriber service to pick up, either immediately or on a schedule (recommend: immediately, but the transcriber processes one session at a time to bound memory).

### 3.3 Memory considerations at capture time

- Never buffer full-session audio in RAM — stream to disk in small chunks.
- **Storage format decision: retain Opus, decode to WAV/PCM only in-memory at transcription time.** Discord delivers voice as Opus already, so re-encoding to WAV at capture time is pure overhead with no accuracy benefit — decode straight to Opus files per speaker, and let the transcriber decode to 16kHz mono PCM on the fly (`loud.cpp`/ffmpeg handle Opus input natively).
- **Disk space comparison (why this matters less now, but still worth doing):**

  | Format | Approx. size per speaker-hour | 3.25hr session, 5 speakers |
  |---|---|---|
  | 16kHz mono WAV (16-bit PCM) | ~115MB/hour | ~1.9GB |
  | Opus (speech-appropriate, ~24–32kbps) | ~11–14MB/hour | ~200–230MB |

  Opus is roughly **8–10x smaller** than uncompressed WAV for the same content. With 16GB RAM and presumably reasonable disk on your own box this isn't a hard constraint either way, but there's no real downside to keeping Opus as the on-disk format — it's free savings and `loud.cpp`/ffmpeg decode it without issue.
- Capture continuously (see decision below) and write in short flush intervals (30–60s) as separate small segment files, both for crash resilience and to avoid ever holding a large in-memory buffer.

### Decision: Continuous capture, no push-to-talk gating
Capture continuously for the whole time a speaker is connected to the voice channel, rather than trying to rely on Discord's own "is speaking" indicator to skip dead air at capture time. Silence trimming and voice-activity detection are handled downstream at the transcription stage (Section 4.3) where there's more context and no risk of corrupting the segment timestamps used for transcript ordering.

---

## 4. Local Transcription Engine

### 4.1 Recommendation: `loud.cpp` (whisper.cpp) as the single transcription engine

Since diarization is already needed for shared streams (Section 6), and `loud.cpp` bundles `whisper.cpp`-based transcription together with CPU/ONNX-based diarization in one tool, the simplest design is to **use `loud.cpp` for every stream, not just shared ones** — there's no real-time requirement pushing toward a faster/lighter engine for the "easy" streams, so running two separate transcription stacks (one for normal streams, one for shared streams) would just be extra complexity for no benefit. For a normal (non-shared) stream, diarization is simply skipped and the whole file is transcribed as one speaker; for a shared stream with `diarize: true`, the diarization pass runs first and each resulting sub-segment is transcribed — all through the same tool and the same on-disk model.

This also means only one transcription/diarization dependency to install, containerize, and keep in sync (rather than `faster-whisper`/CTranslate2 for normal streams plus `loud.cpp` for shared ones). `whisper.cpp`'s underlying model format and quantization options are broadly comparable to `faster-whisper`'s in memory terms; the model-size guidance in 4.2 applies the same way.

### 4.2 Model size vs. memory tradeoff

| Model | Approx RAM (int8/quantized, whisper.cpp via `loud.cpp`) | Notes |
|---|---|---|
| tiny / tiny.en | ~75–150MB | Fast, noticeably worse accuracy on names/jargon |
| base / base.en | ~150–300MB | Reasonable floor for a first attempt |
| small / small.en | ~400–800MB | Comfortable, fast |
| medium / medium.en | ~1.5–2.5GB | **Now realistic given 16GB RAM** — noticeably better accuracy on names/accents than `small`, and memory is no longer the constraint it would have been on a small VPS |
| large-v3 | ~3–4GB+ | Feasible memory-wise on 16GB, but on a 4-core i3-8100 with no GPU acceleration this will be considerably slower per session — worth benchmarking against `medium` before committing to it as the default |

With 16GB RAM and no other heavy services running, memory is comfortably available for **`medium.en`** as the new default, with `small.en` as a fallback if a 3.25-hour session takes uncomfortably long to process on 4 CPU cores. Recommend benchmarking both on one real session recording (wall-clock time is now the main thing worth measuring, not memory) and keeping the model name as a config value either way so it's trivial to switch. `large-v3` is worth an experimental try but likely to be impractically slow on this CPU for routine use.

### 4.3 Processing approach: batch, not streaming

Since real-time isn't required:

1. Process one session at a time, one speaker-file at a time, sequentially (not in parallel) — this bounds peak memory to "one loaded model + one audio segment" rather than N-way parallel loads.
2. Load the Whisper model **once per transcription job**, reuse across all of that session's speaker files, then unload (free the process / let it exit) rather than keeping it resident in a long-lived service — trades a few seconds of model-load latency for a much smaller steady-state memory footprint.
3. Within a speaker's audio, use Whisper's built-in VAD filtering (`loud.cpp`/`whisper.cpp` support VAD filtering) to skip silence rather than doing a separate VAD pass — reduces both compute and spurious "you" / hallucinated-text-on-silence artifacts that Whisper models are known to produce on empty audio.

### Implementation Notes
```python
# transcriber/worker.py (sketch)
# loud.cpp is a compiled CLI tool; invoke it per audio file and parse its JSON output.
import subprocess, json

def transcribe_session(session_id: str, config: dict):
    segments_by_speaker = get_audio_segments_for_session(session_id)  # from DB
    all_lines = []
    for speaker in segments_by_speaker:
        prompt = build_initial_prompt(session_id)  # lexicon injection, see Sec 5
        for audio_file in speaker.files:
            cmd = [
                "loud", audio_file.path,
                "--model", config["whisper_model"],   # e.g. "medium.en"
                "--prompt", prompt,
                "--json", "-",                        # write JSON result to stdout
            ]
            if speaker.map.diarize:
                cmd.append("--diarize")               # only for streams flagged diarize=true
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            parsed = json.loads(result.stdout)
            for seg in parsed["segments"]:
                cluster = seg.get("speaker")  # present only when --diarize was used
                label = (
                    speaker.map.cluster_name_map.get(cluster, f"Speaker {cluster} (stream)")
                    if cluster is not None
                    else speaker.resolved_name
                )
                all_lines.append(TranscriptLine(
                    speaker=label,
                    start=audio_file.session_offset + seg["start"],
                    end=audio_file.session_offset + seg["end"],
                    text=seg["text"].strip(),
                ))
    all_lines.sort(key=lambda l: l.start)
    write_markdown_transcript(session_id, all_lines)
```
Exact CLI flags/JSON schema above are illustrative — confirm against `loud.cpp`'s actual `--help` output and JSON structure once it's installed, since flag names may differ from this sketch.

### Decision: Manual trigger by default, with a configurable auto-on-session-end option
Transcription is started via a manual `/transcribe` command by default. A config flag (e.g. `transcription.auto_on_session_end: true`) allows switching to automatic behavior, where the transcriber job is queued as soon as a session is marked ended. Since memory is no longer the tight constraint it would have been on a VPS, the main reason to prefer manual-by-default is simply control over *when* the multi-hour CPU-bound job runs relative to other things you might be doing on the same box — not memory contention.

---

## 5. Challenge: Unfamiliar Names (People, Places, Fantasy Terms)

Whisper has no idea that "Kethrandir" or "the Sunken Vaults of Ael'thari" are real words in your campaign, and will happily mis-transcribe them as the nearest real-sounding word. Two complementary mitigations:

### 5.1 Initial-prompt lexicon injection (primary mechanism)

Whisper (via `whisper.cpp`/`loud.cpp`) accepts an `initial_prompt` string that biases the decoder toward certain vocabulary without needing any fine-tuning. This is the cheapest, most maintainable fix:

- Maintain a **per-campaign lexicon** — a simple human-editable YAML/text file listing character names, place names, recurring fantasy terms, and NPC names.
- Before each transcription run, build a short prompt string from this lexicon (e.g. a comma-separated list, or a couple of sample sentences using the terms) and pass it as `initial_prompt`.
- Keep the injected prompt short (Whisper's prompt has a limited effective context — a few dozen key terms works much better than dumping an entire 200-entry glossary verbatim every time). Consider prioritizing terms that are either new that session or have historically been mis-transcribed.

### 5.2 Post-processing correction pass (secondary/optional mechanism)

Because latency doesn't matter, add an optional **fuzzy-correction pass** after transcription:

- Take the raw transcript text and the lexicon list.
- For each word/phrase in the transcript, check phonetic/fuzzy similarity (e.g. `rapidfuzz` for string distance, or a phonetic algorithm like Double Metaphone via `metaphone`/`jellyfish`) against lexicon entries.
- Replace close matches below a distance threshold with the canonical lexicon spelling, logging what was changed so a human can spot-check.
- This is a good place — and the *only* recommended place — to optionally allow a local or cloud LLM cleanup pass if the user wants one later (correcting names/grammar from context), since by this point it's operating on **text only**, not audio, so it doesn't violate the "no audio to the cloud" requirement. This should remain an explicit opt-in, off by default.

### 5.3 Building the lexicon over time

Rather than requiring the DM to hand-write a perfect glossary up front:

- Provide a `/lexicon add <term> [aliases...]` Discord command so the DM/players can add names as they come up ("that NPC we just met") directly from Discord, with the entry stored in the `LexiconEntry` table (and optionally mirrored to the human-editable YAML for easy bulk editing).
- After each transcription run, surface a short list of "possible missed proper nouns" — words that were capitalized mid-sentence, or repeated unusual tokens, or flagged as low-confidence by Whisper (`whisper.cpp`/`loud.cpp` expose per-segment confidence/probability info) — as candidates for the DM to review and add to the lexicon. This turns lexicon-building into a lightweight review step rather than upfront manual work.

### Implementation Notes
```yaml
# config/lexicon.yaml (example, human-editable, also DB-backed)
campaign: "Curse of the Sunken Crown"
characters:
  - name: "Kethrandir"
    aliases: ["Keth"]
  - name: "Oshiro Vellweather"
    aliases: ["Oshiro"]
places:
  - name: "Ael'thari"
  - name: "The Sunken Vaults"
terms:
  - "githyanki"
  - "Mordenkainen"
```

```python
def build_initial_prompt(session_id: str, max_terms: int = 40) -> str:
    entries = get_lexicon_terms(session_id, limit=max_terms, prioritize_recent=True)
    # Whisper responds better to prompt phrased as natural text than a bare list
    return "Campaign notes mentioning: " + ", ".join(entries) + "."
```

### Open Questions
- Is there an existing campaign wiki/notes doc (e.g. World Anvil, Notion, a shared Google Doc) that the lexicon could be seeded from automatically? If so, a one-off import script would save manual setup — worth a follow-up conversation once the doc format is known.

---

## 6. Challenge: Multiple Players Sharing One Discord User/Mic

This is the harder problem, since Discord's per-user audio stream gives no signal that two humans are on one mic — from Discord's perspective it's a single audio source.

### 6.1 Design principle: make it a per-session, per-user configuration, not a global assumption

Since who's sharing a webcam changes over time and isn't always the same person, **do not hardcode this anywhere in code** — model it as configurable data:

- A Discord user can be marked, **per session** (not permanently), as "shared" — meaning their audio stream should be attributed to more than one character/player rather than assumed to be a single speaker.
- Recommend a `/speakers` command flow at the start of a session (or editable mid-session), e.g.:
  - `/speakers set-shared @DiscordUser "Alice (as Kethrandir)" "Bob (as Oshiro)"`
  - `/speakers set-single @DiscordUser "Carla (as Elowen)"` (the default/normal case — one Discord user, one player)
  - `/speakers list` — shows current session's mapping for confirmation before recording starts.
- This produces a `SpeakerMap` table keyed by `(session_id, discord_user_id)` → either a single resolved name, or a "shared: true" flag with a list of candidate names for that session.

### 6.2 Distinguishing the two voices on a shared stream: automatic diarization via `loud.cpp`

Manual tagging (players typing or reacting to indicate who's about to speak) was considered and explicitly ruled out — it adds conversational overhead to actual play, which isn't acceptable for two people who are already sharing a mic and just want to talk naturally. The chosen approach instead is **automatic speaker diarization**, run only on the audio streams that need it.

**Tool choice: [`loud.cpp`](https://github.com/thewh1teagle/loud.cpp)** — a project that pairs `whisper.cpp` with CPU-based diarization via `onnxruntime`, using a pyannote-derived model exported for CPU inference rather than requiring full PyTorch/CUDA. This is a good fit for the i3-8100 + UHD 630 box specifically because:
- It runs diarization on CPU via ONNX Runtime, not full `pyannote.audio` + PyTorch — meaningfully lighter to install and run.
- The UHD 630 iGPU has no useful acceleration path for either whisper.cpp or the diarization model here, so there's nothing lost by this being a CPU-only tool.
- It bundles transcription and diarization together, and per the decision in Section 4.1, this design uses `loud.cpp` as the single transcription engine for every stream (not just shared ones) — for a normal stream, diarization is simply skipped and the file is transcribed as one speaker; for a shared stream with `diarize: true`, the diarization pass runs first and each resulting sub-segment is transcribed, all through the same tool.

**How it fits the pipeline:** for a stream not marked "shared," diarization is skipped entirely — the whole stream is one speaker, exactly as today. For a stream marked "shared," the audio segment is run through diarization first to produce "speaker A / speaker B" time-boundaries, and each resulting sub-segment is then transcribed and labeled using the session's configured names for that stream (see 6.1) — cluster "A" and "B" need a one-time per-session mapping to actual names, e.g. via a `/speakers set-shared` sub-option specifying which cluster is which (first-to-speak = A is a reasonable default assumption, correctable after the fact if wrong).

**Configurability (per your preference): diarization is per-stream configurable, default off.** This is modeled as a third piece of per-session `SpeakerMap` data alongside `is_shared`:
- `is_shared: false, diarize: false` — normal single-speaker stream, no diarization (typical case, cheapest).
- `is_shared: true, diarize: true` — the expected combination for an actual shared mic; diarization runs automatically for this stream.
- `is_shared: true, diarize: false` — shared mic acknowledged but diarization deliberately turned off for this stream/session (e.g. testing, or if diarization quality turns out poor for a particular pair of voices); falls back to labeling the whole stream with a combined name or an "Unknown (shared mic)" placeholder for manual cleanup.
- `is_shared: false, diarize: true` — available as a safety net per your preference, e.g. if you're unsure whether a "normal" stream might occasionally pick up a second voice (someone leaning into shot/mic) and want it checked anyway, at the cost of the extra diarization pass for that stream.

This keeps the (comparatively expensive) diarization pass opt-in and targeted, while leaving the option open to run it anywhere it might help, without it ever being forced on by default for the common single-speaker case.

### 6.3 Recommended phased approach

- **Phase 1 (MVP):** Config-driven `is_shared` marking per session (Section 6.1), no diarization yet — shared streams simply get a combined/placeholder label. Validates the rest of the pipeline first.
- **Phase 2:** Add `loud.cpp`-based diarization wired in as described above, with the per-stream `diarize` flag defaulting to off and enabled per-session via the `/speakers` command. This is the target end state for the shared-mic problem, not an optional extra.

### Implementation Notes
```python
# shared/models.py (sketch)
class SpeakerMap(Base):
    session_id: str
    discord_user_id: str
    is_shared: bool
    diarize: bool                 # per-stream, default False
    resolved_names: list[str]     # len 1 if not shared, len N if shared
    cluster_name_map: dict | None # e.g. {"A": "Alice (Kethrandir)", "B": "Bob (Oshiro)"}
```

Attribution logic at transcription time:
- `diarize: false` (whether or not `is_shared`) — transcribe the whole stream as one speaker, label with `resolved_names[0]` (or a combined placeholder if `is_shared` but not yet diarized).
- `diarize: true` — run `loud.cpp`'s diarization pass on the stream first to get speaker-A/speaker-B time boundaries, transcribe each resulting sub-segment (via the same `loud.cpp` tool), and label each using `cluster_name_map`. If `cluster_name_map` hasn't been set yet for the session, default to labeling clusters "Speaker A (stream)" / "Speaker B (stream)" and flag the transcript for a quick manual name-mapping pass.

### Open Questions
- Once a first real shared-mic recording exists, worth doing a quick manual accuracy check of `loud.cpp`'s diarization output against what was actually said, to decide whether the default cluster-to-name mapping heuristic (first-to-speak = A) is good enough or needs a more explicit per-session confirmation step.

---

## 7. Data Model Summary

```python
class Session(Base):
    id: str
    guild_id: str
    voice_channel_id: str
    started_at: datetime
    ended_at: datetime | None
    status: Enum["recording", "queued", "transcribing", "done", "failed"]

class AudioSegment(Base):
    session_id: str
    discord_user_id: str
    ssrc: int
    file_path: str
    session_offset_seconds: float
    duration_seconds: float

class SpeakerMap(Base):
    session_id: str
    discord_user_id: str
    is_shared: bool
    diarize: bool                # per-stream, default False; see Section 6.2
    resolved_names: list[str]
    cluster_name_map: dict | None # e.g. {"A": "Alice (Kethrandir)", "B": "Bob (Oshiro)"}, set once diarization runs

class LexiconEntry(Base):
    campaign_id: str
    term: str
    aliases: list[str]
    category: Enum["character", "place", "term"]
    last_used_at: datetime | None
```

---

## 8. Docker & Deployment

### 8.1 `docker-compose.yml` (sketch)

```yaml
services:
  bot:
    build: ./bot
    restart: unless-stopped
    env_file: .env
    volumes:
      - ./data:/data
      - ./config:/config
    mem_limit: 300m

  transcriber:
    build: ./transcriber
    restart: "no"          # invoked on demand, not a long-running daemon
    env_file: .env
    volumes:
      - ./data:/data
      - ./config:/config
    mem_limit: 3000m         # comfortable headroom for medium.en (Sec 4.2) plus loud.cpp diarization
```

### 8.2 Notes

- `mem_limit` values above are illustrative placeholders — set once real numbers are measured on your box with the chosen Whisper model size. With 16GB total RAM available, these limits exist mainly to catch runaway processes rather than to squeeze into a tight budget.
- Keep the `transcriber` image separate from `bot` so the bot image stays small and doesn't need PyTorch/CTranslate2 installed at all.
- Model weights should be downloaded once and cached in a named volume (not baked into the image) so rebuilding the image doesn't re-download multi-hundred-MB model files.
- `.env` holds the Discord bot token and any other secrets; never commit it.

### Resolved
- Docker Compose confirmed as acceptable for deployment on the home box.

---

## 9. Output Format

Default output: a Markdown transcript per session, e.g. `data/output/<session_id>.md`:

```markdown
# Session: 2026-07-05 — Curse of the Sunken Crown

**Kethrandir (Alice):** [00:03:12] I want to check the door for traps.
**DM:** [00:03:15] Roll me an investigation check.
**Oshiro (Bob, shared mic with Alice):** [00:03:20] Can I help with that?
```

Also consider offering `.srt` output (timestamps in subtitle format) as a secondary artifact — trivial to generate from the same `TranscriptLine` list, and useful if anyone ever wants to sync it against a session recording video.

### Decision: Delivery via Discord
The bot posts the finished transcript back into Discord once transcription completes — either as a file upload in the channel the session was recorded from, or a dedicated thread per session (recommended, to keep the main channel tidy across many sessions). The file also remains on disk under `data/output/` regardless, so it's always available directly on the box as a fallback.

---

## 10. Suggested Build Order (Phase Plan)

1. **Skeleton bot** — joins/leaves VC on command, no recording yet. Validates Discord permissions/setup.
2. **Recording** — per-user WAV capture to disk, session lifecycle (start/stop), SQLite metadata.
3. **Basic transcription** — `loud.cpp` (no `--diarize`) on captured files, no lexicon/shared-mic handling yet, output a plain Markdown transcript with Discord display names as speaker labels. This is the first genuinely useful milestone.
4. **Lexicon injection** — `initial_prompt` building from a manually-populated `lexicon.yaml`, plus the `/lexicon add` command.
5. **Shared-mic handling, phase 1** — `SpeakerMap` (`is_shared`/`diarize` flags) + `/speakers` commands; shared streams without diarization get a combined placeholder label for manual cleanup.
6. **Shared-mic handling, phase 2** — wire in `loud.cpp` diarization for streams with `diarize: true`, plus the cluster-to-name mapping step in `/speakers`.
7. **Polish** — fuzzy lexicon correction pass, `.srt` export, low-confidence-segment review list, wall-clock time tuning (model size, thread count) based on real session runs.

---

## 11. Bot Naming Note

The project/bot is called **"Scribe's Assistant."** One wrinkle worth knowing: Discord distinguishes a bot's **username** (the unique `@handle`, restricted to lowercase letters, numbers, underscores, and periods only — no apostrophes, spaces, *or capitals*) from its **display name** (what actually shows next to its messages in servers, which allows almost any Unicode up to 32 characters, including capitals and apostrophes). So "Scribe's Assistant" works fine as the bot's display name. For the username, `AssistantScribe` is a nice compact form of it, but note Discord will require it entered as all-lowercase (`assistantscribe`) — the platform doesn't support mixed case in the actual handle even though the display name can show any casing/punctuation you like. Recommend `assistantscribe` as the username with `Scribe's Assistant` as the display name.

## 12. Summary of Key Recommendations

- **Discord layer:** `py-cord` for built-in per-user voice receiving.
- **Capture format:** Opus per speaker stream (not WAV), continuous capture with no push-to-talk gating, decoded to PCM only at transcription time.
- **Transcription:** `loud.cpp` (whisper.cpp-based) as the single engine for every stream — normal streams transcribed directly, shared streams diarized then transcribed — model size `medium.en` as the new default given 16GB RAM (fallback to `small.en` if wall-clock time on the 4-core i3-8100 proves too slow for routine use), one session processed sequentially, one speaker-file at a time.
- **Transcription trigger:** manual `/transcribe` command by default, with a config flag for automatic triggering on session end.
- **Names/lexicon:** `initial_prompt` injection from an editable, DB-backed lexicon + optional fuzzy post-correction pass + a Discord command to grow the lexicon over time.
- **Shared mic:** automatic diarization via `loud.cpp` (CPU-based, ONNX Runtime, no PyTorch/CUDA needed) rather than manual tagging — configurable per-stream via a `diarize` flag, default off, so it only runs where explicitly enabled but can be turned on for any stream as a safety net.
- **Delivery:** finished transcripts posted back into Discord (channel or per-session thread), with the file also retained on disk.
- **Containerization:** two Docker services (`bot`, `transcriber`) sharing a data volume via Docker Compose, split specifically so the always-on bot process stays lightweight and the heavier ML work only runs when actually transcribing.
