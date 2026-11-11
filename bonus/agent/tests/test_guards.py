from iow.loop.guards import Guards


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


CFG = """loop:
  cooldown_per_signature_s: 600
  max_heals_per_hour: 3
  max_heals_lifetime: 5
"""


def _guards(config_file, text=CFG, **kw):
    clock = Clock()
    return Guards(config_file(text), clock=clock, **kw), clock


def test_single_flight(config_file):
    g, _ = _guards(config_file)
    assert g.acquire("a") is None
    assert "single-flight" in g.acquire("b")
    g.release()
    assert g.acquire("b") is None


def test_cooldown_per_signature(config_file):
    g, clock = _guards(config_file)
    assert g.acquire("a") is None
    g.release()
    assert "cooldown" in g.acquire("a")
    assert g.acquire("b") is None  # other signatures are not blocked
    g.release()
    clock.t += 601
    assert g.acquire("a") is None


def test_hourly_rate_limit(config_file):
    g, clock = _guards(config_file, CFG.replace("600", "0"))
    for sig in "abc":
        assert g.acquire(sig) is None
        g.release()
        clock.t += 60
    assert "hourly rate limit" in g.acquire("d")
    clock.t += 3600
    assert g.acquire("d") is None


def test_lifetime_cap(config_file):
    g, clock = _guards(config_file, CFG.replace("600", "0").replace("max_heals_per_hour: 3", "max_heals_per_hour: 100"))
    for i in range(5):
        assert g.acquire(str(i)) is None
        g.release()
        clock.t += 10
    assert "lifetime cap" in g.acquire("x")
    clock.t += 10 * 86400
    assert "lifetime cap" in g.acquire("x")  # never resets inside one process


def test_kill_switch_refuses_everything_and_persists(config_file, tmp_path):
    state = tmp_path / "kill.json"
    g, _ = _guards(config_file, state_file=state)
    g.enabled = False
    assert "kill switch" in g.acquire("a")
    g2, _ = _guards(config_file, state_file=state)  # agent restart
    assert g2.enabled is False and "kill switch" in g2.acquire("a")


def test_kill_switch_follows_the_config_file(config_file):
    g, _ = _guards(config_file)
    g.config.path.write_text(CFG + "  enabled: false\n")
    g.config.reload()
    assert "kill switch" in g.acquire("a")


def test_limits_are_read_at_decision_time(config_file):
    g, _ = _guards(config_file)
    assert g.acquire("a") is None
    g.release()
    g.config.path.write_text(CFG.replace("600", "0"))
    g.config.reload()
    assert g.acquire("a") is None  # cooldown shortened by a hot edit, no restart


def test_cooldown_for_signatures_seen_during_a_heal(config_file):
    g, _ = _guards(config_file)
    g.cool_down(["new-error"])
    assert "cooldown" in g.acquire("new-error")
