"""Entry point: uvicorn serves `app.main:app`."""

from fastapi import FastAPI

from app.config import APP_NAME
from app.routes import router
from helpers import greet

app = FastAPI(title=APP_NAME)
app.include_router(router)


@app.get("/welcome")
def welcome() -> dict[str, str]:
    return {"message": greet("visitor")}
