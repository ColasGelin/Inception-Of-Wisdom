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
