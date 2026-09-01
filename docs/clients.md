# Connecting Claude Desktop and Cursor

Both configurations below were checked against the official documentation rather than copied from
tutorials, because the format has changed more than once and stale examples are the usual reason a
server silently fails to appear.

Sources: [MCP — Connect to local MCP servers](https://modelcontextprotocol.io/docs/develop/connect-local-servers)
and [Cursor — Model Context Protocol](https://cursor.com/docs/mcp).

---

## The one rule that causes most failures

**Use absolute paths for `command`.** Neither client runs the server from your project directory:
Claude Desktop launches it from an unspecified working directory, and Cursor's config has no `cwd`
field at all. A bare `memhub-server` resolves only if it happens to be on the PATH the client
inherited, which on Windows it usually is not.

Find the absolute path once:

```bash
python -c "import shutil; print(shutil.which('memhub-server'))"
```

In this repository's virtual environment that is
`C:\Users\<you>\mcp_shared_memory_server\.venv\Scripts\memhub-server.exe`.

---

## Claude Desktop

Settings → Developer → **Edit Config**, which opens:

- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`
- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "memhub": {
      "command": "C:\\Users\\you\\mcp_shared_memory_server\\.venv\\Scripts\\memhub-server.exe",
      "env": {
        "MEMHUB_DATABASE_URL": "postgresql+asyncpg://memhub:memhub@localhost:5435/memhub"
      }
    }
  }
}
```

macOS:

```json
{
  "mcpServers": {
    "memhub": {
      "command": "/Users/you/mcp_shared_memory_server/.venv/bin/memhub-server",
      "env": {
        "MEMHUB_DATABASE_URL": "postgresql+asyncpg://memhub:memhub@localhost:5435/memhub"
      }
    }
  }
}
```

Backslashes must be doubled in JSON on Windows. Quit Claude Desktop **completely** and reopen it —
the config is read at startup.

---

## Cursor

- **Global**: `~/.cursor/mcp.json` (`%USERPROFILE%\.cursor\mcp.json` on Windows)
- **Project-scoped**: `.cursor/mcp.json` in the repository root

Cursor's schema differs from Claude Desktop's in one respect: it takes an explicit `type`.

```json
{
  "mcpServers": {
    "memhub": {
      "type": "stdio",
      "command": "C:\\Users\\you\\mcp_shared_memory_server\\.venv\\Scripts\\memhub-server.exe",
      "env": {
        "MEMHUB_DATABASE_URL": "postgresql+asyncpg://memhub:memhub@localhost:5435/memhub"
      }
    }
  }
}
```

Cursor also supports `envFile` for stdio servers, so the connection string can live in a gitignored
`.env` instead of the config:

```json
{
  "mcpServers": {
    "memhub": {
      "type": "stdio",
      "command": "C:\\Users\\you\\mcp_shared_memory_server\\.venv\\Scripts\\memhub-server.exe",
      "envFile": "C:\\Users\\you\\mcp_shared_memory_server\\.env"
    }
  }
}
```

Reachable from Settings → Tools & MCP → New MCP Server.

---

## Point both clients at the same database

This is the entire point, and it is easy to get wrong: the two configs must carry the **same**
`MEMHUB_DATABASE_URL`. Each client spawns its own server process, and those processes share nothing
else — no memory, no cache, no files. PostgreSQL is the only channel between them.

Point them at different databases and everything still appears to work, while each client quietly
keeps its own private corpus.

---

## Verifying it works

Start the database first — the server starts without it, but every call will return
`BACKEND_UNAVAILABLE`:

```bash
docker compose up -d --wait
```

Then, in Claude Desktop:

> Use the memhub tools. Create a project with slug `demo`, then remember this DECISION:
> "PostgreSQL is the task queue. Redis is intentionally excluded from V1."

And in Cursor, in a completely separate session:

> Search memhub's `demo` project for "queue".

The decision comes back, with `author_client` showing which client wrote it.

---

## Troubleshooting

**The server does not appear.** Run the `command` value by hand in a terminal. If it starts and
waits for input, the path is right and the problem is the client's config. If it reports "not
found", the path is wrong — see the absolute-path rule above.

**Read the server's own logs.** Claude Desktop captures each server's stderr to a dedicated file:

- **Windows**: `%APPDATA%\Claude\logs\mcp-server-memhub.log`
- **macOS**: `~/Library/Logs/Claude/mcp-server-memhub.log`

All of this server's logs are JSON on stderr, so that file is the whole story — startup, the
database it connected to, and every rejected call with its error code. `mcp.log` alongside it covers
connection failures.

**Everything fails with a database error.** The container is not running, or the two clients have
different `MEMHUB_DATABASE_URL` values. Check with:

```bash
docker compose ps
```

**One client sees memories the other does not.** Almost always different database URLs. Failing
that, different projects — ask each client to call `project_use` and compare the returned
`project_id`, which is the canonical identity. Slugs and workspace paths are only hints.

---

## Why stdio, and what changes later

stdio means each client spawns its **own** copy of this server. Two processes, no shared memory, no
shared cache — which is exactly why the concurrency control in this project is real rather than
decorative, and why a compare-and-set in PostgreSQL is the only thing that can adjudicate a
conflicting write.

Streamable HTTP with a single shared process is a later phase. It brings authentication and
per-caller authorisation as its own design work; the service layer does not change, only the entry
point.
