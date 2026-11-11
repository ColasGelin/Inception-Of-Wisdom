from iow.observer.events import (EventStore, excerpt_of, find_exception, normalise,
                                 signature_from_log)

TRACE = """INFO:     Started server process [1]
Traceback (most recent call last):
  File "/usr/local/lib/python3.12/site-packages/uvicorn/importer.py", line 19, in import_from_string
    module = importlib.import_module(module_str)
  File "/app/app/main.py", line 7, in <module>
    from helpers import greet
ModuleNotFoundError: No module named 'helpers'"""


def test_normalise_masks_numbers_hex_paths_timestamps():
    text = "2026-11-11T10:00:00.123Z worker 4123 at 0xdeadbeef failed reading /app/app/x.py line 17"
    assert normalise(text) == "<ts> worker <n> at <hex> failed reading <path> line <n>"


def test_normalise_keeps_identifiers():
    assert normalise("KeyError: 'APP_SECRET_KEY'") == "KeyError: 'APP_SECRET_KEY'"
    assert normalise("No module named 'app.helpers2'") == "No module named 'app.helpers2'"


def test_signature_from_traceback_is_stable_across_runs():
    other_run = TRACE.replace("line 7", "line 9").replace("[1]", "[42]")
    assert signature_from_log(TRACE) == signature_from_log(other_run)
    assert signature_from_log(TRACE) == "ModuleNotFoundError: No module named 'helpers'"


def test_signature_masks_numbers_in_message():
    a = signature_from_log("ValueError: invalid literal for int() with base 10: 'twenty'")
    assert a == "ValueError: invalid literal for int() with base <n>: 'twenty'"


def test_signature_falls_back_to_exit_code_then_last_line():
    assert signature_from_log("just some output\n", exit_code=3) == "exit:3"
    assert signature_from_log("server died on port 8000\n") == "log:server died on port <n>"


def test_find_exception_syntax_error():
    assert find_exception('  File "/app/app/routes.py", line 17\nSyntaxError: expected \':\'') == (
        "SyntaxError", "expected ':'")


def test_excerpt_keeps_whole_traceback():
    lines = [f"noise {i}" for i in range(100)] + TRACE.splitlines()
    excerpt = excerpt_of(lines, max_lines=5)
    assert excerpt.splitlines()[0] == "noise 99"  # two lines of context before the traceback (INFO line + 1)
    assert "Traceback (most recent call last):" in excerpt
    assert excerpt.endswith("No module named 'helpers'")


def _store(crash=30.0, suggestion=300.0):
    return EventStore(lambda: (crash, suggestion))


def test_same_signature_within_window_is_merged():
    s = _store()
    ev1, new1 = s.record("crash", "docker", "E: x", run_id="r1", now=100.0)
    ev2, new2 = s.record("crash", "docker", "E: x", run_id="r2", now=110.0)
    assert new1 and not new2 and ev1 is ev2
    assert ev1.count == 2 and ev1.first_seen == 100.0 and ev1.last_seen == 110.0
    assert len(s.list()) == 1


def test_one_bad_start_reported_by_two_sources_counts_once():
    s = _store()
    s.record("crash", "logs", "E: x", run_id="r1", now=100.0)
    ev, new = s.record("crash", "docker", "E: x", run_id="r1", now=100.5, exit_code=1)
    assert not new and ev.count == 1 and ev.exit_code == 1


def test_window_measured_from_last_seen():
    s = _store(crash=30)
    for t in (0, 25, 50, 75):  # each gap < 30s: still the same event
        ev, _ = s.record("crash", "docker", "E: x", run_id=str(t), now=float(t))
    assert ev.count == 4
    _, new = s.record("crash", "docker", "E: x", run_id="late", now=200.0)
    assert new and len(s.list()) == 2


def test_suggestions_use_the_longer_window():
    s = _store(crash=30, suggestion=300)
    s.record("suggestion", "http", "http:u:4xx", now=0.0)
    _, new = s.record("suggestion", "http", "http:u:4xx", now=200.0)
    assert not new
    s.record("crash", "http", "http:u:refused", now=0.0)
    _, new = s.record("crash", "http", "http:u:refused", now=200.0)
    assert new


def test_kinds_do_not_merge():
    s = _store()
    s.record("crash", "http", "sig", now=0.0)
    _, new = s.record("suggestion", "http", "sig", now=1.0)
    assert new


def test_listeners_see_new_and_updates():
    s = _store()
    seen = []
    s.subscribe(lambda ev, new: seen.append((ev.count, new)))
    s.record("crash", "docker", "E", run_id="a", now=0.0)
    s.record("crash", "docker", "E", run_id="b", now=1.0)
    assert seen == [(1, True), (2, False)]


def test_restart_loop_flag():
    s = _store()
    s.record("crash", "docker", "E", run_id="a", now=10.0)
    ev = s.mark_restart_loop(since=0.0, status="restart loop: 3 starts in 60s")
    assert ev is not None and ev.restart_loop
    assert s.mark_restart_loop(since=0.0, status="again") is None  # flagged once
