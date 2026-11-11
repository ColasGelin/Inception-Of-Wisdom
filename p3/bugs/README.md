# Planted bugs

`make bug BUG=<name>` restores the pristine target files, copies the overlay
`bugs/<name>/` over them, commits the result on `main` in the runtime workspace
as `plant bug: <name>`, and restarts the target.

| name     | file            | what breaks                                                        | expected outcome |
|----------|-----------------|--------------------------------------------------------------------|------------------|
| `syntax` | app/routes.py   | missing `:` after `def health()` -> `SyntaxError` on import        | healed, attempt 1 |
| `import` | app/main.py     | `from helpers import greet` (greet lives in `app/text.py`) -> `ModuleNotFoundError` | healed within 3 attempts |
| `env`    | app/config.py   | `int(os.environ["APP_MAX_NAME_LENGTH"])`, the deployment sets it to `twenty` -> `ValueError` | the right fix is in the deployment env, not the code: usually 3 failed attempts, then a clean rollback |
| `route`  | app/routes.py   | `/health` renamed to `/healthz` -> the probe gets a 404            | suggestion event only, never patched |
| `none`   | (none)          | back to the pristine code                                          | healthy |
