import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from iow.config import ConfigStore  # noqa: E402


@pytest.fixture
def config_file(tmp_path):
    def write(text: str = "") -> ConfigStore:
        path = tmp_path / "iow.config.yml"
        path.write_text(text)
        return ConfigStore(path)
    return write


@pytest.fixture
def git_repo(tmp_path):
    """A throwaway target repository with one commit on main."""
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "__init__.py").write_text("")
    (repo / "app" / "main.py").write_text(
        "from fastapi import FastAPI\n\nfrom app.text import greet\n\napp = FastAPI()\n\n\n"
        "@app.get('/')\ndef index() -> dict:\n    return {'message': greet('world')}\n")
    (repo / "app" / "text.py").write_text(
        "def greet(name: str) -> str:\n    return f'Hello, {name}!'\n")
    for args in (["init", "-q", "-b", "main"], ["add", "-A"],
                 ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "initial"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True)
    return repo
