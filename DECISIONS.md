# Decisions

Choices made where the subject (v1.1) leaves room, with the reading a strict
evaluator would expect.

## Layout
- **Parts are self-contained copies.** `p1` ⊂ `p2` ⊂ `p3` ⊂ `bonus`, each with its own
  `docker-compose.yml`, `agent/`, `target/`, `bugs/` and tests. Shared files are byte-identical
  copies (Docker build contexts do not follow symlinks). A part's features are just the packages
  it contains: `iow.analyst` exists from p2, `iow.loop` from p3, `iow.bonus` in bonus.
  `runtime.py`, `api.py` and the dashboard detect them, so those files are the same in every part.
- **Only one part runs at a time.** The target container is named `iow-target` in every part
  (the probes and config address it by name). `make down PART=pX` before switching.
- **The target's git repository is created at runtime.** The pristine source is committed in
  `pN/target/`; the one-shot `init` service copies it to `pN/.runtime/target/` (gitignored),
  runs `git init`, commits on `main`. It keeps an existing workspace; `make reset` forces a rebuild.
- **`iow.config.yml` stays at `pN/target/iow.config.yml`** (the target project root) and is mounted
  read-only into the agent with the whole pristine directory at `/pristine` (a directory mount,
  because single-file bind mounts keep the old inode when an editor replaces the file, which
  breaks hot reload). It is *not* copied into the workspace, so it is never indexed or patched.
- **Config is polled (1 s, configurable), not watched with inotify**: inotify events do not
  cross Docker Desktop bind mounts reliably. Unknown keys are rejected (a typo such as
  `grace_periode_s` would otherwise silently keep the default).

## Observer
- **"One bad start = one event."** Events are keyed by (kind, signature). The window is measured
  from `last_seen`, so a restart loop keeps bumping one event. The log-pattern detector and the
  Docker `die` event both report the same start; the container's `StartedAt` is used as a run id
  so that one start counts once whatever the number of sources.
- **Probe failures are crashes only while the container runs**, for
  `probe_failures_before_crash` consecutive probes of the same run. While the container is
  exited/restarting, the Docker side already owns the failure; counting it again would make one
  bad start two events. If Docker itself is unreachable, HTTP failures do raise crashes.
- **4xx is a suggestion**, deduplicated over `suggestion_dedup_window_s`, never sent to the loop.
- **Restart loop** = `restarts` container starts within `window_s`. It flags the current crash
  event (`restart loop` badge); if the container loops while exiting 0, it raises its own
  `restart-loop:<container>` crash.
- **The crash excerpt** is read from the container's own log tail on `die` (not from the live
  stream, which can attach after a fast crash) and keeps the whole last traceback.
- `GET /events` is the SSE stream (reference API); the JSON list of events is `GET /events/recent`.
- Infrastructure constants that are not behaviour (SSE heartbeat, Docker API timeouts) are named
  constants. Every timing that shapes what the agent does is in `iow.config.yml`.

## Planted bugs
- `make bug` goes through the agent API (`POST /demo/bug/{name}`) so planting shares the agent's
  workspace lock: during a heal it is refused with HTTP 409 instead of racing the loop's git
  operations. Each plant = pristine code + one overlay from `bugs/<name>/`, committed on `main`.
- **env** reads `APP_MAX_NAME_LENGTH` as an int; the deployment (compose) sets it to `twenty`.
  The real fix belongs in the deployment, outside the source tree, so this bug is the one that
  normally exhausts the attempts and demonstrates the byte-for-byte rollback. A model *can* make
  the code tolerate it (try/except with a default); if it does, the heal is accepted, because the
  symptom is gone, and that is reported honestly.
- **route** renames `/health` to `/healthz`: the probe gets 404, a suggestion, never patched.

## Analyst
- **The manifest lives in Chroma itself**: a `code_files` collection holds one record per file
  (content hash + symbol table as JSON) next to `code_chunks`. One store, one source of truth; the
  manifest record is written last, so a crash mid-file just redoes that file on the next sync.
- **Incremental = content hash**, not mtimes: a `git checkout` that rewrites ten files re-embeds
  only the ones whose bytes changed. Changing the embedding model (stored in the collection
  metadata) is the only case that re-embeds everything.
- **The watcher is watchdog's PollingObserver** (1 s), for the same Docker Desktop inotify reason.
- **Chunks**: Python by AST (module header, each top-level function/class, big classes split into
  methods); files that do not parse (the syntax bug) fall back to splitting at top-level
  `def`/`class`/decorators so they stay searchable. YAML by top-level key, TOML by table, Markdown by
  heading, Dockerfile by instruction group, everything else by blank-line blocks.
- **The model chooses, it does not search.** Candidates come from traceback frames, the symbol table
  (who defines / imports the identifiers, who reads the env vars) and retrieval. The JSON schema
  sent to Ollama makes `files` an enum of those candidates; the answer is validated again.
- **Retrieval alone is not evidence.** If no traceback frame or known identifier points into the
  tree, the diagnosis fails explicitly (`stage: evidence`) without calling the model: picking a file
  because its embedding is close would be a guess.
- **Models are pulled by the agent** through the Ollama API on first start (progress in `make logs`),
  into the `models` volume. The agent refuses a chat model whose `/api/show` parameter count is
  >= 3B, so changing `llm.model` to a bigger model cannot silently break the rule.
- Ollama has no published host port (CPU inference, compose network only), which also avoids
  clashing with a host Ollama on 11434.

## Wisdom Loop
- **File contents never travel inside JSON from the model.** Observed with qwen2.5-coder:1.5b and
  Ollama structured output: the model writes `return {"` without escaping the quote, which closes the
  JSON string and truncates the file (586 -> 83 bytes; the 60 % shrink check caught it every time).
  So the patch step makes one plain-text call per suspect file (temperature 0, fixed seed) asking for
  `SUMMARY: ...` plus the complete file in one code block, and the agent builds the patch JSON
  `{summary, files: [{path, op, content}]}` itself. The patch format, the full-file contents and the
  sanity checks are exactly the subject's; structured output stays on the diagnosis call, where it works.
- **The index helps the model, it never edits for it.** The diagnosis prompt carries facts the symbol
  table can prove ("no module named `helpers` exists", "`greet` is defined in app/text.py"); the patch
  prompt adds where each name of a broken import lives (`from app.text import greet`). That last fact is
  kept out of the diagnosis prompt: it pulled the 1.5B model's choice of file towards the definer.
- **One consistency re-ask.** If the model's chosen files omit the file containing the failing line,
  it is asked once more with that observation; whatever it answers then is final. (The import bug went
  from roughly 50/50 to 3/3 on the right file.)
- **Prompts state the real constraint**: the fix must be a source change; installing packages is not
  possible. Without it the model "fixed" a missing module with `pip install`.
- **Diagnosis summary has a `maxLength`**: unbounded, the model rambled until Ollama returned
  `done: false` (invalid JSON).
- **Snapshot refuses a dirty tree** instead of committing someone's uncommitted edits.
- **What aborts vs retries.** Unusable diagnosis or an unreachable LLM ends the heal at once (rollback;
  retrying the same input cannot help, and the pipeline does not guess). A patch rejected by the sanity
  checks, an unusable patch answer or a failed verify go to the next attempt with that feedback and the
  new error log.
- **VERIFY ignores stale reports**: crash events whose run id is the run that was just replaced do not
  count; crashes from the loop's own restarts are recorded but never trigger a new heal (single-flight),
  and every signature seen during a heal gets a cooldown when it ends.
- **A python syntax check** (`compile`) is added to the four required sanity checks, plus safe-path,
  op/existence and no-op checks: cheap, and it saves a restart on obviously broken code.
- **Kill switch** = a runtime flag initialised from `loop.enabled`, flippable from the dashboard/API and
  persisted in the agent's state volume, so a restart does not silently re-enable healing. Editing
  `loop.enabled` in the file also sets it. A heal already running finishes (healed or rolled back).
- **Lifetime cap and cooldowns are per agent process**, as the subject's "hard cap over the process
  lifetime" says; the hourly window is a real sliding hour.
- **If the agent dies mid-heal**, the history marks the heal `interrupted` and the next start rolls the
  workspace back to the recorded original commit and verifies it.
- **History** = JSON lines in the `agent-data` volume, one full snapshot per transition, so the third tab
  survives restarts and `make export-run` can replay a run.
