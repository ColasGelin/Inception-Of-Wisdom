# Inception-of-Wisdom (IoW)

A local autonomous agent that watches a running service, notices it is broken, finds the code
that probably caused it, asks a small **local** LLM for a fix, applies the patch, commits it to
Git, restarts the service and checks the symptom is gone. If the fix does not take, it rolls back
through Git as if it had never acted.

**observe → think → act → verify**, entirely offline once the models are downloaded:
no remote LLM, no hosted embeddings, no telemetry.

| | |
|---|---|
| Chat model | `qwen2.5-coder:1.5b`: **1,543,714,304 parameters (1.54B, under 3B)**, read from Ollama's `/api/show` and shown in the dashboard header. The agent refuses any configured model with 3B parameters or more. |
| Embeddings | `nomic-embed-text`, served by the same local Ollama |
| Vector DB | ChromaDB `PersistentClient`, incremental, never rebuilt on start |
| Agent | Python 3.12, FastAPI, Docker SDK, in a container |

## Requirements

- Docker with Compose v2 (Docker Engine on Linux, or Docker Desktop on macOS/Windows/WSL2)
- `make`, `curl`, `git`
- About **4 GB free disk** (1.2 GB of models + images)
- Free local ports 8042 (dashboard) and 8081 (the demo target); change them with
  `make up IOW_PORT=9042 IOW_TARGET_PORT=9081`

On **macOS** Docker has no GPU access, so inference runs on the CPU: it works, a diagnosis takes a few
seconds and a patch 5-20 s. To use a native Ollama on the host instead, set
`llm.host: http://host.docker.internal:11434` in `p3/target/iow.config.yml` (and pull the two models there).

## Start

```bash
make up              # PART=p3 is the default: the full system
```

Open **http://127.0.0.1:8042**. On the first start the agent downloads the two models into a Docker
volume (about 1.2 GB, a few minutes); follow it with `make logs` until you see

```
[llm] chat model qwen2.5-coder:1.5b: 1.54B parameters (1,543,714,304), under 3B
[analyst] 9 file(s) changed, 17 chunks embedded in 2.2s
[analyst] watching /workspace/target for changes
```

Later starts reuse the volume and the index. After `make down && make up` the log says:

```
[analyst] 0 files changed, index reused (9 files, 17 chunks, nothing re-embedded)
```

Each part can be graded on its own: `make up PART=p1` (Observer), `PART=p2` (+ Analyst),
`PART=p3` (+ Wisdom Loop), `PART=bonus`. Run one part at a time (`make down PART=...` before switching).

## Drive it

| Command | What happens |
|---|---|
| `make bug BUG=syntax` | missing `:` in `app/routes.py`: crash on start, healed on attempt 1 |
| `make bug BUG=import` | `from helpers import greet` in `app/main.py` (`greet` lives in `app/text.py`): healed |
| `make bug BUG=env` | `app/config.py` reads `APP_MAX_NAME_LENGTH` as an int, the deployment sets it to `twenty`: the real fix is in the deployment, so normally 3 attempts then a clean rollback |
| `make bug BUG=route` | `/health` renamed `/healthz`: the probe gets 404, a **suggestion** event, never patched |
| `make bug BUG=none` | back to the pristine code |
| `make heal-off` / `make heal-on` | global kill switch (also a toggle in the dashboard header) |
| `make reset` | rebuild the target workspace from the pristine source (ChromaDB untouched) |
| `make test` | 127 unit tests (p3) in the agent image, with a fake Docker and a fake LLM (no Ollama needed) |
| `make export-run` | write the latest healed run and the latest rolled-back run to `exports/*.jsonl` |
| `make logs`, `make ps` | follow the logs, list the containers |
| `make down` | stop and remove the containers, keep the volumes |
| `make clean` | `down` + remove the runtime workspace; `make clean ALL=1` also removes the model, Chroma and history volumes |

Every bug is committed on `main` in the runtime workspace (`p3/.runtime/target`, a real Git repo you can
`git log` on the host) as `plant bug: <name>`, then the target is restarted.

## What to watch

**1 · Observer**: target state (running / restarting / exited / missing), Docker daemon connectivity,
each probe's result and latency, live target logs, the agent's own log, and the deduplicated events: a
crash loop is one red card whose count goes up ("9× · 8 duplicates merged"), a 404 is one yellow
suggestion card.

**2 · Analyst**: index state (files, chunks, embedding model, what the last sync re-embedded), the model
and its exact parameter count, a retrieval box returning the top-k chunks with cosine scores, and
"Diagnose" on any crash event: the suspect file, the paragraph, the JSON handed to the loop, the candidates
the index found before asking the model and why, the chunks the model saw.

**3 · Wisdom Loop**: IDLE / HEALING (attempt, step) / KILL SWITCH ON, the safety bounds (single-flight,
heals this hour, heals this process, active cooldowns), the live timing values from `iow.config.yml`, and
every heal with every attempt: steps, diagnosis, patch JSON, sanity checks, commit hash and message,
post-restart health, and for a rollback the five proofs (HEAD, status, diff, blob-by-blob, agent commits
unreachable). Refused triggers (cooldown, kill switch...) are listed below.

## Bonus: `make up PART=bonus`

Everything from p3, plus:

- **Pull requests on a local Gitea** (container `iow-gitea`, offline, port 3042). A one-shot `gitea-init`
  service creates the admin user, an API token and the `iow/target` repository with no clicks. After every
  successful heal the agent pushes `main` and `iow/auto-heal` and opens (or updates) a PR into `main` titled
  from the crash, with the diagnosis and patch JSON in the body. **The agent never merges**: a human reviews.
  The PR link appears in the third tab. `make gitea PART=bonus` prints the URL and the generated login.
- **Richer third tab**: each patch rendered as a diff that the dashboard computes itself from the full
  before/after files (never a model-written diff), the per-chunk relevance (cosine) of what the model saw,
  the candidate ranking, and a timeline of past heals with success counts and mean time to heal.

## Configuration: `p3/target/iow.config.yml`

All timings live there and are **hot-reloaded** (polled every second): edit, save, and the next decision
uses the new values. No code change, no restart. To shorten the grace period:

```yaml
loop:
  grace_period_s: 3
```

The agent logs `[config] iow.config.yml changed, new values active`, the third tab shows `3`, and the next
verify says `watching the target for 3s`. An invalid file (bad YAML, unknown key, out-of-range value) keeps
the last good config and shows the error in a red banner.

## How it works

```
            ┌────────────────────────── agent container (UID = workspace owner) ───────────────────────────┐
 docker.sock│ OBSERVER                      ANALYST                           WISDOM LOOP                   │
 ──────────►│ events() thread ─┐            Chroma PersistentClient            guards: kill switch,         │
            │ logs(stream) ────┼─► dedup ─► (chunks + manifest + symbols)      single-flight, cooldown,     │
            │ state poll ──────┘   events │ watchdog -> hash diff -> re-embed   hourly rate, lifetime cap    │
 HTTP probes│ httpx probes ────────────►  │                                     snapshot sha, checkout -B    │
 ──────────►│                    crash ───┴──► parse traceback -> symbols ->    iow/auto-heal                │
            │                             retrieval -> candidates -> LLM picks  ×3: diagnose, patch, sanity, │
            │                             {summary, files}  ────────────────►  apply, commit, restart, verify│
            │ FastAPI + SSE  ◄── dashboard (3 tabs)                             else: reset --hard + clean,  │
            └──────────────┬──────────────────────────────┬───────────────────  restart, verify blob-by-blob ┘
                           │ /workspace/target (rw)       │ http://ollama:11434
                 ┌─────────▼──────────┐          ┌────────▼─────────┐
                 │ runtime workspace  │          │ Ollama (local)   │  qwen2.5-coder:1.5b
                 │ git repo, bind-    │          │ models volume    │  nomic-embed-text
                 │ mounted at /app in │          └──────────────────┘
                 │ the target         │
                 └────────────────────┘
```

1. **Observe**: Docker events and the log stream run in background threads with their own retry/backoff,
   HTTP probes in an asyncio task. Failures become events keyed by a normalised signature
   (`ModuleNotFoundError: No module named 'helpers'`, `http:<url>:4xx`, `exit:137`).
2. **Think**: the index parses the traceback, maps `/app/app/main.py` to `app/main.py`, looks identifiers
   up in the symbol table (who defines `greet`, who imports `helpers`, who reads `APP_MAX_NAME_LENGTH`),
   runs retrieval, and hands the model a numbered list of real files. The JSON schema makes `files` an enum
   of that list: the model can only choose. No usable answer = explicit failure, never a guess.
3. **Act**: the model returns each suspect file in full; the agent builds the patch JSON, runs the sanity
   checks (≤ 3 files, no placeholder, ≤ 60 % shrink, no prompt markers, safe paths, parses), writes
   atomically, commits on `iow/auto-heal`, restarts the target.
4. **Verify**: healed = up for `grace_period_s` with no new crash event, then every probe answers. Otherwise
   the new error goes back to the Analyst, up to 3 attempts; then `git reset --hard` to the snapshot, restart,
   and a blob-by-blob proof that the tree is the original commit.

More: [DECISIONS.md](DECISIONS.md) (every interpretation of the subject), [DEFENSE.md](DEFENSE.md)
(10-minute demo + likely questions), [CHECKLIST.md](CHECKLIST.md) (each requirement and how it was verified).

## Repository layout

```
p1/  p2/  p3/  bonus/      self-contained snapshots: docker-compose.yml, agent/, target/, bugs/
  agent/iow/               the agent (observer/, analyst/, loop/, api.py, llm.py, config.py, store.py)
  agent/web/               the dashboard (vanilla JS + SSE, no build step)
  agent/tests/             unit tests (fake Docker, fake LLM, temp git repos)
  target/                  pristine hello service + iow.config.yml
  bugs/<name>/             overlays planted by `make bug`
exports/                   `make export-run` output (git-ignored)
```

Never committed: model weights (Docker volume `iow-models`), the Chroma data (volume `chroma`), the
runtime workspace (`pN/.runtime/`, git-ignored), `.env` files (generated by `make up`).
