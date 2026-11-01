"""coach-instructions-mcp

Small MCP server (Streamable HTTP) that serves the instructions for "Coach Claudi" from a
Git repository (e.g. gitlab.com).

* Keeps a local clone up to date (GitLab webhook + periodic sync + sync on access when the
  last sync is older than COACH_SYNC_TTL seconds).
* Read tools: get_instructions, list_instruction_files, read_instruction_file,
  search_instructions, instructions_history, sync_instructions.
* Write tools (only with COACH_WRITE=1): write_instruction_file,
  replace_in_instruction_file. Every change is committed and pushed; the Git history is the
  undo tool.

Tool descriptions and tool output are German on purpose (that is what Claude sees).

Configured via environment variables only, see deploy/coach-mcp.env.example.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path, PurePosixPath

import anyio
import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.responses import JSONResponse, Response

log = logging.getLogger("coach-mcp")

# --------------------------------------------------------------------------- Configuration


def _env(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        raise SystemExit(f"Environment variable {name} is missing (see deploy/coach-mcp.env.example)")
    return value or ""


REPO_URL = _env("COACH_REPO_URL", required=True)
REPO_DIR = Path(_env("COACH_REPO_DIR", "/var/lib/coach-mcp/repo"))
BRANCH = _env("COACH_BRANCH", "main")
SSH_KEY = _env("COACH_SSH_KEY")  # path to the (private) deploy key; empty = default SSH config
URL_TOKEN = _env("COACH_URL_TOKEN", required=True)  # secret path segment of the MCP URL
BEARER_TOKEN = _env("COACH_BEARER_TOKEN")  # optional additional check: Authorization: Bearer ...
WEBHOOK_TOKEN = _env("COACH_WEBHOOK_TOKEN")  # GitLab "Secret token" (header X-Gitlab-Token)
WRITE_ENABLED = _env("COACH_WRITE", "0") == "1"
SYNC_TTL = float(_env("COACH_SYNC_TTL", "20"))  # seconds; on access pull at most this often
SYNC_INTERVAL = float(_env("COACH_SYNC_INTERVAL", "300"))  # background sync
HOST = _env("COACH_HOST", "127.0.0.1")
PORT = int(_env("COACH_PORT", "8765"))
ALLOWED_HOSTS = [h.strip() for h in _env("COACH_ALLOWED_HOSTS").split(",") if h.strip()]
GIT_NAME = _env("COACH_GIT_NAME", "Coach Claudi")
GIT_EMAIL = _env("COACH_GIT_EMAIL", "coach-claudi@localhost")
TEXT_EXTENSIONS = {".md", ".txt"}
MAX_FILE_BYTES = 200_000
MAX_SEARCH_HITS = 50

if len(URL_TOKEN) < 24:
    raise SystemExit("COACH_URL_TOKEN must be at least 24 characters long (e.g. openssl rand -hex 24)")

# --------------------------------------------------------------------------- Git layer

_lock = threading.RLock()
_state = {"last_sync": 0.0, "last_error": ""}


class GitError(RuntimeError):
    pass


def _git(*args: str, timeout: int = 60, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = GIT_NAME
    env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = GIT_EMAIL
    if SSH_KEY:
        known_hosts = REPO_DIR.parent / "known_hosts"
        env["GIT_SSH_COMMAND"] = (
            f"ssh -i {SSH_KEY} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new "
            f"-o UserKnownHostsFile={known_hosts}"
        )
    cmd = ["git", *args] if args[0] == "clone" else ["git", "-C", str(REPO_DIR), *args]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {args[0]} Zeitueberschreitung") from exc
    if check and proc.returncode != 0:
        raise GitError(f"git {args[0]} fehlgeschlagen: {(proc.stderr or proc.stdout).strip()}")
    return proc


def ensure_clone() -> None:
    with _lock:
        if (REPO_DIR / ".git").is_dir():
            return
        REPO_DIR.parent.mkdir(parents=True, exist_ok=True)
        _git("clone", "--branch", BRANCH, REPO_URL, str(REPO_DIR), timeout=180)
        log.info("Cloned repository to %s", REPO_DIR)


def sync(force: bool = False) -> str:
    """Fetches the remote branch. On errors the last state is kept."""
    with _lock:
        if not force and time.time() - _state["last_sync"] < SYNC_TTL:
            return "cached"
        try:
            ensure_clone()
            _git("fetch", "--prune", "origin", BRANCH, timeout=90)
            _git("reset", "--hard", f"origin/{BRANCH}")
            _state["last_sync"] = time.time()
            _state["last_error"] = ""
            return "ok"
        except GitError as exc:
            _state["last_error"] = str(exc)
            log.warning("Sync failed: %s", exc)
            return f"error: {exc}"


def head_info() -> str:
    proc = _git("log", "-1", "--format=%h|%cI|%s", check=False)
    if proc.returncode != 0 or not proc.stdout.strip():
        return "unbekannt"
    sha, date, subject = proc.stdout.strip().split("|", 2)
    return f'{sha}, {date}, "{subject}"'


def tracked_files() -> list[str]:
    out = _git("ls-files", "-z").stdout
    names = [n for n in out.split("\0") if n]
    return sorted(
        n for n in names if PurePosixPath(n).suffix.lower() in TEXT_EXTENSIONS
    )


def _safe_path(rel: str, must_exist: bool) -> Path:
    """Validates a relative path (guards against path tricks, .git and wrong extensions)."""
    p = PurePosixPath(rel.strip().replace("\\", "/"))
    if not rel.strip() or p.is_absolute() or ".." in p.parts or ".git" in p.parts:
        raise ValueError(f"Ungueltiger Pfad: {rel!r}")
    if p.suffix.lower() not in TEXT_EXTENSIONS:
        raise ValueError(f"Nur Dateien mit Endung {sorted(TEXT_EXTENSIONS)} erlaubt")
    full = (REPO_DIR / p).resolve()
    if REPO_DIR.resolve() not in full.parents:
        raise ValueError(f"Pfad ausserhalb des Repositorys: {rel!r}")
    if must_exist and str(p) not in tracked_files():
        raise FileNotFoundError(f"Datei nicht im Repository: {rel!r} (list_instruction_files zeigt alle)")
    return full


def _commit_and_push(rel: str, message: str) -> str:
    """Commits the file and pushes. On a push conflict: rebase once and retry."""
    message = message.strip() or f"Update {rel}"
    if not message.endswith("(by Claude)"):
        message += " (by Claude)"
    _git("add", "--", rel)
    if _git("diff", "--cached", "--quiet", check=False).returncode == 0:
        return "Keine Aenderung (Inhalt identisch), nichts committet."
    _git("commit", "-m", message)
    push = _git("push", "origin", f"HEAD:{BRANCH}", timeout=90, check=False)
    if push.returncode != 0:
        try:
            _git("pull", "--rebase", "origin", BRANCH, timeout=90)
            _git("push", "origin", f"HEAD:{BRANCH}", timeout=90)
        except GitError as exc:
            _git("rebase", "--abort", check=False)
            _git("reset", "--hard", f"origin/{BRANCH}", check=False)
            raise GitError(f"Push nicht moeglich, Aenderung verworfen: {exc}") from exc
    _state["last_sync"] = time.time()
    return f"Committet und gepusht: {head_info()}"


# --------------------------------------------------------------------------- Tool logic (synchronous)


def _header() -> str:
    suffix = f" | Hinweis: letzter Abgleich fehlgeschlagen ({_state['last_error']})" if _state["last_error"] else ""
    return f"Repository-Stand: {head_info()}{suffix}"


def do_get_instructions() -> str:
    sync()
    files = tracked_files()
    files.sort(key=lambda n: (n.lower() != "readme.md", n.lower()))
    parts = [f"# Anweisungen fuer Coach Claudi\n{_header()}\nDateien: {len(files)}\n"]
    for name in files:
        text = (REPO_DIR / name).read_text(encoding="utf-8", errors="replace")
        parts.append(f"\n\n=== DATEI: {name} ===\n\n{text.rstrip()}\n")
    return "".join(parts)


def do_list() -> str:
    sync()
    rows = []
    for name in tracked_files():
        size = (REPO_DIR / name).stat().st_size
        rows.append(f"{name}  ({size} Bytes)")
    return _header() + "\n" + "\n".join(rows)


def do_read(path: str) -> str:
    sync()
    full = _safe_path(path, must_exist=True)
    return f"{_header()}\n\n{full.read_text(encoding='utf-8', errors='replace')}"


def do_search(query: str, regex: bool) -> str:
    sync()
    if not query:
        raise ValueError("query darf nicht leer sein")
    pattern = re.compile(query if regex else re.escape(query), re.IGNORECASE)
    hits: list[str] = []
    for name in tracked_files():
        for no, line in enumerate((REPO_DIR / name).read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if pattern.search(line):
                hits.append(f"{name}:{no}: {line.strip()}")
                if len(hits) >= MAX_SEARCH_HITS:
                    return "\n".join(hits) + f"\n... (auf {MAX_SEARCH_HITS} Treffer begrenzt)"
    return "\n".join(hits) if hits else "Keine Treffer."


def do_history(path: str, limit: int) -> str:
    sync()
    limit = max(1, min(limit, 50))
    args = ["log", f"-{limit}", "--format=%h  %cI  %an  %s"]
    if path:
        _safe_path(path, must_exist=False)
        args += ["--", path]
    out = _git(*args).stdout.strip()
    return out or "Keine Commits gefunden."


def do_sync() -> str:
    result = sync(force=True)
    return f"Abgleich: {result}\n{_header()}"


def do_write(path: str, content: str, message: str) -> str:
    if not content.strip():
        raise ValueError("Leerer Inhalt nicht erlaubt (zum Loeschen bitte direkt im Repository arbeiten)")
    if len(content.encode("utf-8")) > MAX_FILE_BYTES:
        raise ValueError(f"Datei groesser als {MAX_FILE_BYTES} Bytes")
    with _lock:
        sync(force=True)
        if _state["last_error"]:
            raise GitError(f"Kein aktueller Stand, Schreiben abgebrochen: {_state['last_error']}")
        full = _safe_path(path, must_exist=False)
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content if content.endswith("\n") else content + "\n", encoding="utf-8")
        return _commit_and_push(str(PurePosixPath(path.strip())), message)


def do_replace(path: str, old: str, new: str, message: str) -> str:
    if not old:
        raise ValueError("old darf nicht leer sein")
    with _lock:
        sync(force=True)
        if _state["last_error"]:
            raise GitError(f"Kein aktueller Stand, Schreiben abgebrochen: {_state['last_error']}")
        full = _safe_path(path, must_exist=True)
        text = full.read_text(encoding="utf-8")
        count = text.count(old)
        if count != 1:
            raise ValueError(f"old muss genau einmal vorkommen (gefunden: {count}x)")
        full.write_text(text.replace(old, new, 1), encoding="utf-8")
        return _commit_and_push(str(PurePosixPath(path.strip())), message)


# --------------------------------------------------------------------------- MCP tools

mcp = MCPServer(
    "coach-instructions",
    instructions=(
        "Zentrale, versionierte Anweisungen fuer den Trainer-Coach 'Claudi'. "
        "Rufe zu Beginn jedes neuen Chats und vor relevanten Planungen, Aenderungen oder "
        "Auswertungen get_instructions auf und wende alle Anweisungen an."
    ),
)

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)


async def _run(fn, *args):
    try:
        return await anyio.to_thread.run_sync(fn, *args)
    except (ValueError, FileNotFoundError, GitError) as exc:
        raise ValueError(str(exc)) from exc


@mcp.tool(annotations=READ_ONLY)
async def get_instructions() -> str:
    """Liefert ALLE Anweisungen fuer Coach Claudi (alle Markdown-Dateien des Repositorys,
    README zuerst) inklusive Commit-Stand. Zu Beginn jedes Chats aufrufen."""
    return await _run(do_get_instructions)


@mcp.tool(annotations=READ_ONLY)
async def list_instruction_files() -> str:
    """Listet die Dateien der Anweisungen mit Groesse und aktuellem Repository-Stand."""
    return await _run(do_list)


@mcp.tool(annotations=READ_ONLY)
async def read_instruction_file(path: str) -> str:
    """Liest eine einzelne Datei der Anweisungen (Pfad wie von list_instruction_files)."""
    return await _run(do_read, path)


@mcp.tool(annotations=READ_ONLY)
async def search_instructions(query: str, regex: bool = False) -> str:
    """Durchsucht alle Anweisungsdateien (ohne Gross-/Kleinschreibung); Treffer als Datei:Zeile."""
    return await _run(do_search, query, regex)


@mcp.tool(annotations=READ_ONLY)
async def instructions_history(path: str = "", limit: int = 10) -> str:
    """Zeigt die letzten Commits (optional nur fuer eine Datei), neueste zuerst."""
    return await _run(do_history, path, limit)


@mcp.tool(annotations=READ_ONLY)
async def sync_instructions() -> str:
    """Holt sofort den neuesten Stand aus dem Git-Remote (normalerweise nicht noetig)."""
    return await _run(do_sync)


if WRITE_ENABLED:

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False))
    async def write_instruction_file(path: str, content: str, commit_message: str) -> str:
        """Legt eine Anweisungsdatei an oder ersetzt sie komplett (nur .md/.txt) und pusht den
        Commit. Vorher read_instruction_file nutzen, damit nichts verloren geht. Nur nach
        ausdruecklicher Zustimmung des Nutzers aufrufen. '(by Claude)' wird an die
        Commit-Nachricht angehaengt."""
        return await _run(do_write, path, content, commit_message)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False))
    async def replace_in_instruction_file(path: str, old: str, new: str, commit_message: str) -> str:
        """Ersetzt in einer Anweisungsdatei genau eine Textstelle (old muss exakt einmal vorkommen)
        und pusht den Commit. Bevorzugt gegenueber write_instruction_file fuer kleine Aenderungen.
        Nur nach ausdruecklicher Zustimmung des Nutzers aufrufen."""
        return await _run(do_replace, path, old, new, commit_message)


# --------------------------------------------------------------------------- HTTP wrapper


def _describe_rpc(body: bytes) -> str:
    """Summarizes a JSON-RPC request body for the log, e.g. 'tools/call get_instructions'.

    Only method and tool name are logged, never arguments (they may contain instruction text).
    """
    try:
        payload = json.loads(body)
    except ValueError:
        return "unparsable body"
    parts = []
    for msg in payload if isinstance(payload, list) else [payload]:
        if not isinstance(msg, dict):
            continue
        method = msg.get("method") or ("response" if "result" in msg or "error" in msg else "?")
        params = msg.get("params")
        name = params.get("name") if method == "tools/call" and isinstance(params, dict) else None
        parts.append(f"{method} {name}" if isinstance(name, str) else str(method))
    return ", ".join(parts) or "empty batch"


class Gate:
    """ASGI wrapper: secret URL path, optional bearer token, GitLab webhook, health check."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope["path"]
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}

        if path == "/healthz":
            await JSONResponse({"status": "ok"})(scope, receive, send)
            return

        if path == "/hook/gitlab":
            ok = (
                scope["method"] == "POST"
                and WEBHOOK_TOKEN
                and hmac.compare_digest(headers.get("x-gitlab-token", ""), WEBHOOK_TOKEN)
            )
            if not ok:
                await Response(status_code=404)(scope, receive, send)
                return
            threading.Thread(target=sync, kwargs={"force": True}, daemon=True).start()
            await Response("sync started", status_code=202)(scope, receive, send)
            return

        segment = path.split("/")[1] if path.startswith("/") and len(path.split("/")) > 1 else ""
        if not hmac.compare_digest(segment, URL_TOKEN):
            await Response(status_code=404)(scope, receive, send)
            return
        if BEARER_TOKEN and not hmac.compare_digest(
            headers.get("authorization", ""), f"Bearer {BEARER_TOKEN}"
        ):
            await Response(status_code=401)(scope, receive, send)
            log.warning("MCP %s rejected: wrong bearer token", scope["method"])
            return
        await self._serve_logged(scope, receive, send, headers)

    async def _serve_logged(self, scope, receive, send, headers):
        """Passes the request on and logs one line per request, without the secret path."""
        body = bytearray()
        status = 0
        start = time.monotonic()

        async def receive_tapped():
            message = await receive()
            if message["type"] == "http.request":
                body.extend(message.get("body", b""))
            return message

        async def send_tapped(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive_tapped, send_tapped)
        finally:
            log.info(
                "MCP %s %s -> %s in %d ms (client: %s)",
                scope["method"],
                _describe_rpc(bytes(body)) if body else "-",
                status or "no response",
                (time.monotonic() - start) * 1000,
                headers.get("x-anthropic-client") or headers.get("user-agent", "?"),
            )


def _background_sync() -> None:
    while True:
        time.sleep(SYNC_INTERVAL)
        sync(force=True)


def build_app():
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=bool(ALLOWED_HOSTS),
        allowed_hosts=ALLOWED_HOSTS,
        allowed_origins=[],
    )
    inner = mcp.streamable_http_app(
        streamable_http_path=f"/{URL_TOKEN}/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=security,
    )
    return Gate(inner)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ensure_clone()
    log.info("Initial sync: %s", sync(force=True))
    threading.Thread(target=_background_sync, daemon=True).start()
    log.info("Writing %s, listening on %s:%s", "ENABLED" if WRITE_ENABLED else "disabled", HOST, PORT)
    # access_log=False: keep the secret URL path out of log files
    uvicorn.run(build_app(), host=HOST, port=PORT, access_log=False, log_level="info")


if __name__ == "__main__":
    main()
