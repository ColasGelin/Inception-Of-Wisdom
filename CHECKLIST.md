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

## Phase 3: Analyst (p2)
- [x] First start indexes the target (live: `9 file(s) changed, 17 chunks embedded`); `make down && make up`
  does not re-embed (live log: `[analyst] 0 files changed, index reused (9 files, 17 chunks, nothing re-embedded)`;
  test `test_reopen_reuses_the_index_without_embedding`).
- [x] Editing one file re-embeds only that file within ~2 s; deleting a file removes its chunks
  (live: `re-embedded app/text.py`, `removed app/extra.py from the index`; tests
  `test_editing_one_file_reembeds_only_that_file`, `test_deleting_a_file_removes_its_chunks`).
- [x] Retrieval returns top-k chunks with cosine scores in the Analyst tab (live + screenshot).
- [x] Diagnosis names the right file for each planted bug, with a sensible paragraph, as JSON
  `{summary, files}` (live, qwen2.5-coder:1.5b): syntax -> `app/routes.py` (missing colon line 17),
  import -> `app/main.py` (module `helpers` not found), env -> `app/config.py` (`int('twenty')` on
  `APP_MAX_NAME_LENGTH`). `route` raises no crash, so there is nothing to diagnose (suggestion only).
- [x] Unusable model answers are explicit failures (mocked LLM: empty summary, no file, invented path,
  empty object, LLM exception: `test_unusable_model_answer_is_an_explicit_failure`; live: a log with no
  evidence returns `stage: evidence` and never calls the model).
- [x] Tests: chunker (`test_chunker.py`), incremental index / hash manifest (`test_index.py`), path
  mapping + answer validation + identifier extraction (`test_diagnose.py`, `test_symbols.py`).
