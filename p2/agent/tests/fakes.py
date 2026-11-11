"""Fake Docker client: enough of the SDK surface for the observer and the loop."""

from __future__ import annotations

from docker.errors import NotFound


class FakeContainer:
    def __init__(self, name: str = "iow-target", status: str = "running", logs: bytes = b""):
        self.name = name
        self.status = status
        self.exit_code = 0
        self.started_at = "2026-01-01T00:00:00.000000000Z"
        self._logs = logs
        self.restarts = 0
        self.on_restart = None  # callable(container) to simulate the next run

    @property
    def attrs(self) -> dict:
        return {"State": {"Status": self.status, "Restarting": self.status == "restarting",
                          "ExitCode": self.exit_code, "StartedAt": self.started_at},
                "RestartCount": self.restarts}

    def logs(self, **kwargs):
        if kwargs.get("stream"):
            return iter([])
        return self._logs

    def restart(self, timeout: int = 10) -> None:
        self.restarts += 1
        self.status = "running"
        self.started_at = f"2026-01-01T00:00:{self.restarts:02d}.000000000Z"
        if self.on_restart:
            self.on_restart(self)


class FakeContainers:
    def __init__(self, client: "FakeDocker"):
        self._client = client

    def get(self, name: str) -> FakeContainer:
        if self._client.down:
            raise ConnectionError("docker socket unavailable")
        c = self._client.container
        if c is None or c.name != name:
            raise NotFound(f"No such container: {name}")
        return c


class FakeDocker:
    def __init__(self, container: FakeContainer | None = None):
        self.container = container if container is not None else FakeContainer()
        self.down = False
        self.containers = FakeContainers(self)

    def ping(self) -> bool:
        if self.down:
            raise ConnectionError("docker socket unavailable")
        return True

    def events(self, **kwargs):
        if self.down:
            raise ConnectionError("docker socket unavailable")
        return iter([])


class FakeEmbedder:
    """Deterministic bag-of-words vectors; counts what it was asked to embed."""

    DIM = 64

    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, texts: list[str]) -> list[list[float]]:
        import re
        self.calls.append(texts)
        out = []
        for t in texts:
            v = [0.0] * self.DIM
            for w in re.findall(r"[a-z_]+", t.lower()):
                v[sum(map(ord, w)) % self.DIM] += 1.0
            v[0] += 0.01  # never a zero vector
            out.append(v)
        return out

    @property
    def embedded(self) -> int:
        return sum(len(c) for c in self.calls)


class FakeLlm:
    """Returns canned JSON answers in order; records prompts."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.prompts: list[str] = []

    def chat_json(self, system: str, user: str, schema: dict):
        self.prompts.append(user)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer, {"model": "fake", "elapsed_s": 0.0}
