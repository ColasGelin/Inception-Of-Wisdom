# Acceptance checklist

Each item says how it was verified. "live" = run for real with `docker compose`.

## Phase 0: repo skeleton
- [x] `.gitignore` covers `.runtime/`, Chroma data, model files, `__pycache__`, `.env`:
  `git status --ignored` after a full run shows only ignored runtime paths.
- [x] Root `Makefile`, `README.md`, `DECISIONS.md`.
- [x] `git status` clean after commit; `git ls-files | xargs du -ch` stays small (no weights, no DB).

## Phase 1: target service
- [x] `make up PART=p1` serves `/` and `/health` (live: `curl 127.0.0.1:8081/` -> `{"app":"hello-iow",...}`, `/health` -> `{"status":"ok"}`).
- [x] `make bug BUG=syntax` -> `SyntaxError: expected ':'` traceback in `app/routes.py` (live).
- [x] `make bug BUG=import` -> `ModuleNotFoundError: No module named 'helpers'` from `app/main.py` (live).
- [x] `make bug BUG=env` -> `ValueError: invalid literal for int() with base 10: 'twenty'` (live).
- [x] `make bug BUG=route` -> probe `/health` gets 404 (live).

## Phase 2: Observer (p1)
- [x] `/status`, `/logs`, `/events` (SSE) work; the Observer tab shows health, probes, live logs, events
  (live + headless-browser screenshot, no JS errors).
- [x] One planted bug = one crash event whose count rises (live: import bug, "9× · 8 duplicates merged";
  test `test_one_bad_start_reported_by_two_sources_counts_once`, `test_window_measured_from_last_seen`).
- [x] `route` -> one suggestion event, never acted on (live; test `test_probe_4xx_is_a_suggestion_never_a_crash`).
- [x] `docker stop iow-target` / `docker rm -f iow-target` / recreate: agent stays up, state goes
  `exited` -> `missing` -> `running`, the log stream resumes (live).
- [x] Docker daemon disconnect: simulated socket failure, the watcher backs off and reconnects
  (`test_agent_survives_docker_socket_failure_and_reconnects`). Restarting Docker Desktop was not
  done on this machine because it hosts other projects' containers.
- [x] Unit tests for signature normalisation and dedup windows (`tests/test_events.py`).
