import threading
import time

from fakes import FakeContainer, FakeDocker

from iow.observer.docker_watch import DockerWatcher
from iow.observer.events import EventStore
from iow.observer.http_probe import HttpProber, classify

FAST = """
target: { probe_interval_s: 0.05, probe_failures_before_crash: 2 }
observer: { reconnect_backoff_max_s: 0.05, log_settle_s: 0 }
"""


def _store():
    return EventStore(lambda: (30.0, 300.0))


def _wait(pred, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_agent_survives_docker_socket_failure_and_reconnects(config_file):
    cfg = config_file(FAST)
    fake = FakeDocker()
    calls = {"n": 0}

    def factory(timeout):
        calls["n"] += 1
        if calls["n"] <= 3:
            raise ConnectionError("Error while fetching server API version")
        return fake

    w = DockerWatcher(cfg, _store(), client_factory=factory)
    threading.Thread(target=w._state_loop, daemon=True).start()
    assert _wait(lambda: w.state.docker_ok and w.state.state == "running")
    fake.down = True  # daemon goes away while connected
    assert _wait(lambda: not w.state.docker_ok and w.state.state == "unknown")
    fake.down = False
    assert _wait(lambda: w.state.docker_ok and w.state.state == "running")


def test_missing_container_then_back(config_file):
    cfg = config_file(FAST)
    fake = FakeDocker()
    store = _store()
    w = DockerWatcher(cfg, store, client_factory=lambda t: fake)
    container, fake.container = fake.container, None
    w.refresh_state(fake)
    assert w.state.state == "missing" and w.state.docker_ok
    assert any(e.kind == "info" and e.signature.startswith("missing:") for e in store.list())
    fake.container = container
    w.refresh_state(fake)
    assert w.state.state == "running"


def test_die_event_raises_one_crash_with_traceback(config_file):
    cfg = config_file(FAST)
    trace = b'Traceback (most recent call last):\n  File "/app/app/routes.py", line 17\nSyntaxError: expected \':\'\n'
    fake = FakeDocker(FakeContainer(status="exited", logs=trace))
    store = _store()
    w = DockerWatcher(cfg, store, client_factory=lambda t: fake)
    die = {"Action": "die", "Actor": {"Attributes": {"exitCode": "1"}}}
    w.handle_docker_event(die, fake)
    w.handle_docker_event(die, fake)  # same container run reported twice
    crashes = [e for e in store.list() if e.kind == "crash"]
    assert len(crashes) == 1 and crashes[0].count == 1
    assert crashes[0].signature == "SyntaxError: expected ':'"
    assert "Traceback" in crashes[0].log_excerpt and crashes[0].exit_code == 1


def test_clean_exit_is_not_a_crash(config_file):
    cfg = config_file(FAST)
    fake = FakeDocker(FakeContainer(status="exited"))
    store = _store()
    w = DockerWatcher(cfg, store, client_factory=lambda t: fake)
    w.handle_docker_event({"Action": "die", "Actor": {"Attributes": {"exitCode": "0"}}}, fake)
    assert not [e for e in store.list() if e.kind == "crash"]


def test_restart_loop_without_errors_raises_a_crash(config_file):
    cfg = config_file(FAST + "\n")
    fake = FakeDocker()
    store = _store()
    w = DockerWatcher(cfg, store, client_factory=lambda t: fake)
    for _ in range(3):
        w.handle_docker_event({"Action": "start"}, fake)
    crashes = [e for e in store.list() if e.kind == "crash"]
    assert len(crashes) == 1 and crashes[0].restart_loop


def test_error_pattern_in_logs_raises_crash(config_file):
    cfg = config_file(FAST)
    store = _store()
    w = DockerWatcher(cfg, store, client_factory=lambda t: FakeDocker())
    for line in ["Traceback (most recent call last):", '  File "/app/app/x.py", line 1',
                 "KeyError: 'APP_SECRET_KEY'"]:
        w.logs.add(0.0, line, "run1")
        w.check_line(line, "run1")
    assert _wait(lambda: any(e.kind == "crash" for e in store.list()))
    ev = store.list()[0]
    assert ev.source == "logs" and ev.signature == "KeyError: 'APP_SECRET_KEY'"


def test_classify():
    assert classify(200, None) == "ok"
    assert classify(404, None) == "4xx"
    assert classify(503, None) == "5xx"
    assert classify(None, "refused") == "refused"


def _prober(cfg, state="running"):
    store = _store()
    w = DockerWatcher(cfg, store, client_factory=lambda t: FakeDocker())
    w.state.docker_ok, w.state.state, w.state.started_at = True, state, "run1"
    return HttpProber(cfg, store, w), store


def _r(cls, status=None):
    return {"url": "http://t/health", "status": status, "class": cls, "latency_ms": 1, "ts": 0}


def test_probe_4xx_is_a_suggestion_never_a_crash(config_file):
    p, store = _prober(config_file(FAST))
    for _ in range(5):
        p.handle(_r("4xx", 404))
    evs = store.list()
    assert len(evs) == 1 and evs[0].kind == "suggestion" and evs[0].count == 5


def test_probe_failure_needs_consecutive_failures_while_running(config_file):
    p, store = _prober(config_file(FAST))
    p.handle(_r("refused"))
    assert not store.list()
    p.handle(_r("ok", 200))
    p.handle(_r("refused"))
    assert not store.list()  # streak was reset by the success
    p.handle(_r("refused"))
    evs = store.list()
    assert len(evs) == 1 and evs[0].kind == "crash" and evs[0].signature == "http:http://t/health:refused"


def test_probe_failures_while_container_down_belong_to_docker_side(config_file):
    p, store = _prober(config_file(FAST), state="restarting")
    for _ in range(5):
        p.handle(_r("refused"))
    assert not store.list()
