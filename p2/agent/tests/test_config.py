import pytest
from pydantic import ValidationError

from iow.config import Config, parse


def test_defaults_match_the_subject():
    c = Config()
    assert c.loop.max_attempts == 3 and c.loop.branch == "iow/auto-heal"
    assert c.llm.model == "qwen2.5-coder:1.5b"


def test_unknown_keys_are_rejected():
    with pytest.raises(ValidationError):
        parse("loop:\n  grace_periode_s: 3\n")


def test_max_attempts_capped_at_three():
    with pytest.raises(ValidationError):
        parse("loop:\n  max_attempts: 5\n")


def test_hot_reload_applies_new_values(config_file):
    store = config_file("observer:\n  crash_dedup_window_s: 30\n")
    seen = []
    store.on_change(lambda old, new: seen.append(new.observer.crash_dedup_window_s))
    store.path.write_text("observer:\n  crash_dedup_window_s: 5\n")
    assert store.reload()
    assert store.get().observer.crash_dedup_window_s == 5 and seen == [5]


def test_invalid_yaml_keeps_last_good_config(config_file):
    store = config_file("observer:\n  crash_dedup_window_s: 12\n")
    store.path.write_text("observer: [unclosed\n")
    assert not store.reload()
    assert store.get().observer.crash_dedup_window_s == 12
    assert store.error
    store.path.write_text("observer:\n  crash_dedup_window_s: 7\n")
    assert store.reload() and store.error is None


def test_missing_file_uses_defaults(tmp_path):
    from iow.config import ConfigStore
    store = ConfigStore(tmp_path / "nope.yml")
    assert store.get() == Config() and "not readable" in store.error
