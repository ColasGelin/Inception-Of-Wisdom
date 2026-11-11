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

## Phase 4: Wisdom Loop (p3)
- [x] `syntax` heals by itself on attempt 1: commit on `iow/auto-heal`, healthy after the grace period,
  visible in the third tab (live, several runs: e.g. heal `14c2faff`, attempt 1, `up for 20s with no new
  crash; probes / 200, /health 200`; screenshot of tab 3).
- [x] `import` heals within 3 attempts (live: heal `666d708f` healed on attempt 1 with
  `from helpers import greet` -> `from app.text import greet`; offline 3/3 runs of diagnosis + patch also
  produced that exact one-line fix).
- [x] `env`: 3 attempts, then rollback; workspace equals the original commit byte for byte (live: heal
  `0584e4a4`: `head_matches, status_clean, diff_empty, blobs_match, agent_commits_unreachable` all true,
  `main` checked out, target restarted on the original code and crash-looping exactly as before).
- [x] Every sanity check has a unit test rejecting a crafted bad patch (`test_patch.py`: >3 files,
  placeholders (7 shapes), >60 % shrink, retrieval markers (5 shapes), unsafe paths (6 + symlink),
  wrong op / no-op, Python that does not parse).
- [x] Single-flight, per-signature cooldown, hourly rate limit, lifetime cap and kill switch each have a
  test (`test_guards.py`, `test_wisdom.py::test_kill_switch_refuses_new_heals`). The kill switch works
  from the dashboard (headless browser click -> API reports `enabled: false`), survives an agent restart,
  and `make heal-off` / `make heal-on` drive the same API.
- [x] `grace_period_s: 3` in `p3/target/iow.config.yml` takes effect without touching code or restarting
  the agent (live: `[config] iow.config.yml changed`, next heal `[verify] watching the target for 3s`;
  test `test_grace_period_is_read_from_config_at_verify_time`).
- [x] Tests run on a temporary git repository with a fake Docker target and a fake LLM (`test_wisdom.py`,
  `test_git_ops.py`); `make test` needs neither Ollama nor Docker access: 123 tests.
- [x] Stress (all live):
  - plant a bug during a heal -> HTTP 409 "a heal is in progress", nothing written;
  - kill the target mid-verify -> that attempt fails (`exit:137`, excerpt limited to the killed run),
    the next attempt finds no evidence and stops, rollback verified, target running;
  - stop Ollama -> `/analyst/search` answers 503 "embedding model unreachable", the heal ends with an
    explicit "LLM unreachable" diagnosis failure and a verified rollback;
  - invalid YAML / out-of-range value -> last good config kept, error shown in the banner and log;
  - 20 000 error lines in 0.2 s -> one event (count 1), `/status` still answers in ~3 ms, agent CPU ~3 %,
    the triggered heal refuses to guess ("no indexed file matches anything in the error log").
