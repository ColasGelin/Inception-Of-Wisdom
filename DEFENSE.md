# Defense guide

## 10-minute demo

Before the peer arrives: `make up` once so the models are downloaded, then `make bug BUG=none`.
Optionally shorten the wait: set `grace_period_s: 5` in `p3/target/iow.config.yml`.

| min | Command | What the peer sees |
|---|---|---|
| 0 | `make down && make up`, open http://127.0.0.1:8042 | Header: target `running`, docker `connected`, model `qwen2.5-coder:1.5b · 1.54B params`, auto-heal `on`. `make logs` shows `[analyst] 0 files changed, index reused`: no re-embedding. |
| 1 | Tab 2: search `where is the greeting built?` | Top-k logical chunks (`greet`, `welcome`...) with cosine scores. |
| 2 | `make bug BUG=route` | Tab 1: one yellow **suggestion** `http://iow-target:8000/health -> 404`; its count rises, it is never patched; tab 3 stays IDLE. |
| 3 | `make bug BUG=syntax` | Tab 1: one red crash card `SyntaxError: expected ':'`, `restart loop` badge, count rising. Tab 3: HEALING, steps light up: diagnose · llm → patch · llm → sanity → apply → commit → restart → verify → **HEALED** on attempt 1. Expand: diagnosis, patch JSON, 9 sanity checks, commit hash. |
| 5 | `git -C p3/.runtime/target log --oneline --graph --all` and `git -C p3/.runtime/target show --stat HEAD` | The agent's commit on `iow/auto-heal` above `plant bug: syntax` on `main`; one line changed. |
| 6 | `make bug BUG=import` | Healed: `from helpers import greet` becomes `from app.text import greet`. Tab 2 → Diagnose on that event shows why: traceback frame, "`greet` is defined in app/text.py". |
| 7 | `make bug BUG=env` | 3 attempts (each with the new error log), then **CLEAN FAILURE: rolled back**: five green proofs (HEAD, status, diff, blob-by-blob, agent commits unreachable). Target restarted on the original code (crash-looping, as before). |
| 8 | `git -C p3/.runtime/target status; git -C p3/.runtime/target log --oneline --all -3` | Clean tree, `main` checked out, `iow/auto-heal` points at the original commit. |
| 9 | `make heal-off`, `make bug BUG=syntax` | Header turns red; tab 3: "Refused triggers: kill switch is off". `make heal-on`. |
| 9.5 | edit `grace_period_s: 3`, save | `[config] iow.config.yml changed`, tab 3 shows 3, next verify waits 3 s. |
| 10 | `make test` | 127 tests, no Ollama needed. |

Bonus (`make up PART=bonus`): after a heal, tab 3 links to the pull request on the local Gitea
(http://127.0.0.1:3042/iow/target/pulls), with the diff rendered by the dashboard and a timeline of heals.

Extra if asked: `docker stop iow-target` / `docker start iow-target` (agent survives, state goes
`exited` → `running`); `docker stop iow-ollama` then a bug (explicit "LLM unreachable", clean rollback);
break the YAML (red banner, last good config kept); `make export-run`.

## Questions a peer is likely to ask

**The agent mounts `/var/run/docker.sock`. Is that not root on the host?**
Yes. Access to the Docker socket is root-equivalent: whoever can talk to it can start a privileged
container mounting `/`. Mitigations here: the agent's code only calls `containers.get(<configured
name>)`, `.logs`, `.attrs`, `.restart` and `events(filters=container)`; it never creates containers or
pulls images. The process runs as the unprivileged host UID (not root), joined to the socket's group only.
The image has no shell tools beyond the Python base and git, no secrets, and the dashboard binds to
127.0.0.1 only. In production one would put a socket proxy in front (e.g. allow only
`GET /containers`, `GET /events`, `POST /containers/<id>/restart`); for a local, offline project this is
documented rather than added.

**Why does ChromaDB never rebuild?**
`PersistentClient` on a named volume. A second collection `code_files` holds a manifest (content hash +
symbol table) per file. On start the agent hashes the tree and compares: only changed files are
re-embedded, deleted files are removed, the rest is reused (`0 files changed, index reused`). The manifest
record is written after the chunks, so a crash mid-file just redoes that file. The only full re-embed is when
`llm.embedding_model` changes, because vectors from two models are not comparable; the model name is stored in
the collection metadata to detect it.

**How do you stop the model from inventing paths?**
The model never proposes paths. The index builds the candidate list (traceback frames mapped from
`/app/...` to indexed files, symbol-table hits, retrieval), and Ollama's structured output gets a JSON schema
where `files` is an `enum` of those candidates, so the sampler cannot even emit another string. The answer is
validated again (non-empty summary, at least one file, every file in the index and in the list). Anything
else is `{"ok": false, "reason": ...}` and the loop stops. If no traceback frame or known identifier points
into the code, the model is not called at all: retrieval similarity alone is not evidence.

**Why are diffs not accepted?**
A small model gets hunk headers and context lines wrong, and a diff applied to a slightly different file
fails or, worse, applies somewhere else. A complete file can be checked before anything touches the disk:
it parses, it is not a placeholder, it did not lose 60 % of its bytes, it carries no prompt markers. Writing
it is a temp file + `os.replace`. And Git, not the patch, is what undoes it.

**Then why is the patch call not structured JSON like the diagnosis?**
Measured: the 1.5B model, asked for code inside a JSON string, writes `return {"` without escaping the
quote; the JSON string closes there and the file is truncated (586 → 83 bytes). So each file comes back as
plain text in a code block and the agent builds the patch JSON around it. The format, full contents and checks
are the subject's (see DECISIONS.md).

**How is the rollback byte-for-byte?**
Before the first write the loop records the original branch and commit hash. Rollback never applies inverse
patches: `git reset --hard <sha>` on the heal branch, checkout of the original branch, `reset --hard` again,
`git clean -fd`, and the heal branch is moved back to `<sha>` so the agent commits are unreachable from every
ref. Then it proves it: HEAD == sha, `git status --porcelain` empty, `git diff --quiet <sha>`, and every file of
`git ls-tree -r <sha>` re-hashed with `git hash-object` equals the recorded blob. The target is restarted on that
tree. All five results are shown in tab 3 and tested on a temporary repository.

**What stops a runaway loop?**
Single-flight (one heal at a time; crashes caused by its own restarts are recorded, not acted on), a cooldown
per crash signature (also applied to every signature seen during the heal), a sliding hourly limit, a lifetime
cap per agent process, at most 3 attempts per heal, and a global kill switch (dashboard, `make heal-off`, or
`loop.enabled: false`) that persists across agent restarts. Every limit is read from `iow.config.yml` at
decision time.

**Why no remote LLM?**
The subject grades it 0, and it is not needed: the index does most of the work, so a 1.5B model only has to
pick from a short list and rewrite one small file. The only network peers of the agent are the Docker socket,
the target's HTTP port and the Ollama container on the compose network. Chroma runs with
`anonymized_telemetry=False`, httpx clients use `trust_env=False` so no host proxy is involved, and models are
pulled once from Ollama's registry into a local volume (the only download, at first start).

**What if the model is bigger than 3B?**
At start (and on every config change) the agent reads `general.parameter_count` from Ollama's `/api/show` and
refuses a model with 3,000,000,000 parameters or more. Note `qwen2.5-coder:3b` is 3.09B, so it is refused.

**What happens when the target container disappears, or Docker restarts?**
The three Docker threads each have their own retry loop with exponential backoff (ceiling in the config).
State goes `missing` or `unknown (docker unreachable)`, an info event is raised, nothing crashes, and
everything resumes when the container or daemon returns. Tested with a fake client that fails the socket.

**Why does the env bug roll back instead of healing?**
The defect is in the deployment (`APP_MAX_NAME_LENGTH=twenty` in compose), outside the source tree the agent
may change. A model could make the code tolerate it (try/except with a default); if it did, the symptom would
be gone and the heal accepted. With qwen2.5-coder:1.5b it usually tries `int(os.environ.get(..., 40))`, which
still fails, three times, so the run shows the rollback.

**Is the target's file ownership right?**
`make up` writes the host `id -u`/`id -g` to `pN/.env`; both images are built with that UID/GID and run as it,
so the agent's commits and patched files belong to the same user as the target and the host. The one-shot
`init` container runs as root only to create the workspace and `chown` it to that UID.
