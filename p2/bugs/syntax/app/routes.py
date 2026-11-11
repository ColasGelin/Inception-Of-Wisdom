"""HTTP routes of the hello service."""

from fastapi import APIRouter

from app.config import APP_NAME, MAX_NAME_LENGTH
from app.text import greet, shout

router = APIRouter()


@router.get("/")
def index() -> dict[str, str]:
    return {"app": APP_NAME, "message": greet("world"), "try": "/hello/{name}"}


@router.get("/health")
def health() -> dict[str, str]
    return {"status": "ok"}


@router.get("/hello/{name}")
def hello(name: str, loud: bool = False) -> dict[str, str]:
    message = greet(name[:MAX_NAME_LENGTH])
    return {"message": shout(message) if loud else message}
