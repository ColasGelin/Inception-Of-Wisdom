import asyncio

from test_wisdom import FIXED, answer, run_heal  # noqa: F401  (fixtures reused below)
from test_wisdom import setup  # noqa: F401

from iow.bonus import gitea


def _heal():
    return {"id": "h1", "signature": "SyntaxError: expected ':'", "branch": "iow/auto-heal",
            "original": {"branch": "main", "sha": "a" * 40},
            "attempts": [{"n": 1, "outcome": "healed", "commit": "b" * 40,
                          "diagnosis": {"summary": "missing colon", "files": ["app/routes.py"]},
                          "patch": {"summary": "added the colon", "files": [{"path": "app/routes.py", "op": "modify", "content": "x = 1\n"}]},
                          "health": {"reason": "up for 20s with no new crash"}}]}


def test_pr_body_carries_diagnosis_and_patch_json_and_says_not_merged():
    body = gitea.pr_body(_heal())
    assert "Not merged automatically" in body and '"files": [' in body and '"op": "modify"' in body
    assert "attempt 1: healed (`bbbbbbbb`)" in body


def test_publish_failure_is_recorded_not_raised(tmp_path, monkeypatch, config_file):
    monkeypatch.setattr(gitea, "TOKEN_FILE", tmp_path / "missing-token")
    heal = _heal()
    asyncio.run(gitea.GiteaPublisher(config_file("")).publish(heal))
    assert "not bootstrapped" in heal["pr_error"] and "pr" not in heal


def test_after_heal_hooks_run_only_for_healed_runs(setup):  # noqa: F811
    make, ws = setup
    seen = []

    async def hook(heal):
        seen.append(heal["state"])

    from iow.loop import wisdom
    original_init = wisdom.WisdomLoop.__init__

    def init(self, rt):
        original_init(self, rt)
        self.after_heal.append(hook)
    wisdom.WisdomLoop.__init__ = init
    try:
        run_heal(make(answer(FIXED)))
        run_heal(make(*[answer(FIXED.replace("'hi'", f"'hi {i}'") + "BUG = BUG\n") for i in range(3)]))
    finally:
        wisdom.WisdomLoop.__init__ = original_init
    assert seen == ["healed"]


def test_attempt_records_the_original_contents_for_the_diff(setup):  # noqa: F811
    make, ws = setup
    before = (ws / "app" / "main.py").read_text()
    _, heal = run_heal(make(answer(FIXED)))
    assert heal["attempts"][0]["originals"] == {"app/main.py": before}
