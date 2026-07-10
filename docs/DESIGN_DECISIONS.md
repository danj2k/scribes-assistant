# Design Decisions

Supplementary to `dnd-transcription-bot-design.md`. These capture
questions raised during initial review and the decisions made in response.

---

## 1. Audio Format: WAV (Py-cord limitation)

**Decision**: Accept WAV audio from Py-cord's voice receive callbacks.

**Context**: Py-cord decodes Opus to PCM/WAV internally before passing
data to application callbacks. The original design proposed saving raw
Opus to avoid decode-reencode overhead, but this is not possible with
Py-cord. The only library that exposes raw Opus (`discord-ext-voice-recv`)
is alpha quality and not suitable for this project.

**Impact**: Audio capture files will be WAV format (larger on disk than
Opus). This is acceptable given the project's scope and hardware.

---

## 2. Concurrent Sessions: Not Required

**Decision**: Do not design for concurrent recording sessions.

**Context**: The bot is intended for a single D&D group only. At most one
session will be active at a time. There is no need to handle multiple
simultaneous voice channel recordings.

**Impact**: The capture, session management, and transcription flows can
all assume a single active session. No queuing, multiplexing, or
resource contention handling is needed for concurrent sessions.

---

## 3. Transcription Engine: sherpa-onnx (replacing loud.cpp)

**Decision**: Use `sherpa-onnx` as the transcription engine instead of
`loud.cpp`.

**Rationale**:
- `loud.cpp` has not been updated in several years, raising maintenance
  and reliability concerns.
- `sherpa-onnx` has active development and Python bindings available.

**Caveats**:
- Python bindings are available but the API differs from other
  transcription libraries (e.g., faster-whisper). The transcription
  module will need to accommodate these differences.
- `sherpa-onnx` must be clamped to the number of available cores.
  Without explicit thread limiting it will consume threads
  aggressively, which is problematic on the target hardware
  (4 physical cores).

**Impact**: Sections of the original design referencing `loud.cpp`
CLI invocations, JSON output parsing, and job state files must be
reworked to use `sherpa-onnx` Python bindings instead. See
`dnd-transcription-bot-design.md` section 3.5 for the areas affected.

---

## 4. Transcriber Job Queue: Polling Loop (Decoupled from Main Library)

**Decision**: Implement a polling-based job queue, run as a separate
process from the main transcription worker.

**Rationale**: A file-watcher cannot detect when recording has finished
or when a session ends. Polling is needed to check session state.
Keeping the polling loop separate from the transcription worker means
the large `sherpa-onnx` libraries are only loaded when there is actual
work to do.

**Impact**: The bot process polls for completed sessions (e.g. by
checking a SQLite flag or state file). When a job is found, it signals
the transcription worker to load models and process. The worker loads
models at start of each batch and can be idle otherwise.

---

## 5. Transcript Delivery: Straightforward Discord Upload

**Decision**: Upload the completed transcript to Discord as a file
attachment (or embed). Use a Discord thread per session for
organisation, as suggested in the original design.

**Context**: Discord thread creation is a lightweight API call and does
not impose any CPU or memory overhead on the bot. The 4-core hardware
constraint affects transcription processing, not Discord interactions.
Delivering the transcript is a simple HTTP POST to upload the file —
trivial regardless of available resources.

---

## 6. Session Identification: Timestamp-Based

**Decision**: Use a timestamp-based session identifier, normalised to be
filesystem-safe.

**Format**: Session IDs will be derived from the UTC date and time of
session start, formatted for use as filenames. For example:
`2026-07-08T1930` (ISO 8601, colons replaced with nothing or
underscores to be filesystem-safe).

**Impact**: Session directories, audio files, and transcript files all
use this identifier. It provides natural chronological ordering and
human readability.

---

## 7. Deployment: Docker with Persistent Model Weights

**Decision**: Deploy via Docker Compose using a Python slim base image.
Model weights are stored in a mounted volume to survive container
rebuilds.

**Architecture**:
- `Dockerfile` based on `python:<version>-slim`
- `docker-compose.yml` orchestrates the bot and transcriber services
- A named volume or bind mount exposes model weights to the container(s)
  at a fixed path (e.g. `/models`)
- First run downloads weights to the mounted location; subsequent runs
  (including container rebuilds) reuse the cached weights

**Impact**: The transcription worker must be aware of the model weights
mount path. Download/setup logic should check for existing weights
before attempting to download. The Dockerfile should install
`sherpa-onnx` and any compiled dependencies but not bundle model weights.


---

## 8. Py-cord Version: 2.8.0 (Community Fork)

**Decision**: Use `py-cord` 2.8.0 (the actively maintained community fork
at github.com/Pycord-Development/pycord).

**Context**: The original py-cord project (Pycord Development) has not
released since 2.6.1 (September 2024). The community fork at the same
GitHub organisation has continued active development and released 2.8.0
(May 2026) with Python 3.14 support, DAVE E2EE for voice, and ongoing
bug fixes. This is the current stable release.

**Impact**: Install via `py-cord>=2.8.0`. Voice receive is compatible with
the original design. Note that py-cord uses its own slash command API

---

## 9. Lexicon Storage Format: YAML

**Decision**: Store the lexicon as a YAML file (`lexicon.yaml`) rather
than JSON.

**Rationale**: YAML is more human-readable and easier to hand-edit,
which matters since the lexicon will be maintained manually by the DM.
YAML also supports comments, which allows annotating entries without
changing the data structure.

**Schema**:
```yaml
terms:
  - word: Aboleth
    description: "Powerful aberration, ancient evil, tentacled horror"
    added_by: discord_user_id
    added_at: "2026-07-08T19:30:00Z"
corrections:
  - original: "aboleth"
    replacement: "Aboleth"
    added_by: discord_user_id
    added_at: "2026-07-08T19:30:00Z"
```

**Impact**: The lexicon module must read/write YAML. The `pyyaml`
library will be a dependency of both containers (used by the bot for
lexicon commands, and by the transcriber for prompt injection).

---

## 10. Levenshtein Distance Library: Levenshtein

**Decision**: Use the `Levenshtein` package (C-backed Python bindings
for the Welford-Levenshtein algorithm).

**Rationale**: Lightweight, fast (C extension), actively maintained,
and widely used. `rapidfuzz` is heavier (full fuzzy matching toolkit)
and unnecessary for this use case. `python-Levenshtein` is the same
package under an older name.

**Default threshold**: 0.2 — strict enough to avoid false positives on
short fantasy names, permissive enough to catch common mishearings.
Configurable via `config.yaml`.

**Impact**: Add `Levenshtein` as a dependency of the transcriber
container.

---

## 11. Configuration Format: YAML

**Decision**: Use a single `config.yaml` file for all non-sensitive
settings, mounted into both Docker containers.

**Rationale**: YAML is human-readable, supports comments, and aligns
with the lexicon format choice. Docker secrets (bot token) are
file-based and mounted separately at `/run/secrets/`.

**Impact**: Both containers need `pyyaml` as a dependency. A
`config.yaml.example` is provided with documented defaults.
