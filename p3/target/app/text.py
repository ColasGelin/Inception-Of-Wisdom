"""Text helpers used by the routes."""

from app.config import GREETING


def greet(name: str) -> str:
    return f"{GREETING}, {name}!"


def shout(text: str) -> str:
    return text.upper() + "!"
