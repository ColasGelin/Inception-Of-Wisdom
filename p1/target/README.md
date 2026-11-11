# hello target

A deliberately tiny FastAPI service that the IoW agent watches and heals.

- `GET /` greeting, `GET /health` liveness, `GET /hello/{name}`, `GET /welcome`
- Settings come from environment variables (`app/config.py`).
