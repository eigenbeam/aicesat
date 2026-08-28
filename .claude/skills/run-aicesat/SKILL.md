---
name: run-aicesat
description: Build, start, stop, and view the aicesat widget UI (Explore/Lake/Scene) in a browser. Use when asked to run aicesat, start the server, show/open/screenshot the icesat viewer, or take a look at the app.
---

The widget is a self-contained page served over plain HTTP by a Python process
(no Node, no separate frontend build). Drive it with
`.claude/skills/run-aicesat/smoke.sh` to get a live server, then view it with
`claude-in-chrome` (`chromium-cli` is not installed on this host).

All paths below are relative to the repo root.

## Prerequisites

Nothing beyond Python 3.13 and `uv`. If `uv` isn't on `PATH`:

```bash
python3 -m pip install --user uv
export PATH="$HOME/.local/bin:$PATH"
```

`smoke.sh` does this automatically if `uv` is missing.

## Run (agent path)

```bash
.claude/skills/run-aicesat/smoke.sh start
```

This installs `uv` if needed, runs `uv sync`, launches
`scripts/serve.py` in the background, polls `http://127.0.0.1:8765/`
until it responds, and prints the page `<title>` as confirmation.
Server log: `/tmp/aicesat_serve.log`. Override the port with
`AICESAT_PORT`.

To actually look at the page, use the `claude-in-chrome` tools:

```
navigate http://127.0.0.1:8765/
computer screenshot
```

The Explore tab (globe) is the default view; the "Lake" tab is next to
it in the top bar.

When done:

```bash
.claude/skills/run-aicesat/smoke.sh stop
```

## Run (human path)

```bash
uv run scripts/serve.py
```

Blocks in the foreground; open `http://127.0.0.1:8765/` in a browser,
Ctrl-C to stop.

## Test

```bash
uv run pytest
```

---

## Gotchas

- **Do not background `uv run aicesat-server` to view the UI.** That's
  the `pyproject.toml` entry point (`aicesat.server:main`) — it's an
  MCP *stdio* server: it starts the same HTTP widget thread, logs
  `widget server on http://...`, then blocks reading stdin. Launched
  with `&` and no attached stdin, that read hits EOF immediately and
  the whole process (HTTP thread included) exits right after printing
  its "up" log line — `curl` then gets connection-refused even though
  the log looks healthy. `scripts/serve.py` is the widget-only variant
  built for exactly this case; it sleeps forever instead of reading
  stdin.
- **`chromium-cli` is not installed on this host** (`command not
  found`) — there's no Node/npm either. Use the `claude-in-chrome`
  MCP tools instead (`navigate`, `computer screenshot`,
  `read_console_messages`); they were what actually verified the UI
  during development of this skill.
- **Killing the port's listener can leave orphans.** `index.py` and
  `planner.py` fan work out to a `ProcessPoolExecutor`. If a
  background job (index build, scene planning) is in flight when you
  kill the server, its worker processes get reparented to `init`
  instead of exiting, and linger as
  `<repo>/.venv/bin/python3 ... multiprocessing ... --multiprocessing-fork`.
  `smoke.sh stop` sweeps these with `pgrep -f
  "$REPO_ROOT/.venv/bin/python3.*multiprocessing"` in addition to
  killing the port's listener — don't just `kill` the port PID and
  assume it's clean.
- **No Earthdata token needed just to view the UI shell.** The globe,
  area-selection tools, and Lake grid render fine without one. A token
  at `~/.edl/token.prod` (or `EARTHDATA_TOKEN`) is only needed for
  "Check coverage" / "Build scene" to actually fetch ICESat-2/GLAS
  data.

## Troubleshooting

- **`curl: (7) Failed to connect ... Connection refused` right after
  the log shows `widget server on http://...`**: you backgrounded
  `aicesat-server` instead of `scripts/serve.py` — see the first
  Gotcha above.
- **`command not found: uv`**: not on `PATH` in this shell; `smoke.sh`
  installs it via `pip install --user uv`, but a fresh shell still
  needs `export PATH="$HOME/.local/bin:$PATH"` before calling `uv`
  directly (e.g. for `uv run pytest`).
