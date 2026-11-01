"""Local end-to-end test without GitLab: uses a local bare repository as the "remote" and a
fake GitLab for the OAuth token checks.

Run:  python test_local.py
"""

import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

HERE = Path(__file__).parent
TOKEN = "t" * 32
HOOK = "hooksecret"
APP_ID = "app-123"
# fake GitLab: access token -> (application uid, scopes, username)
GITLAB_TOKENS = {
    "good": (APP_ID, ["read_user"], "Yokuha"),
    "other-app": ("app-999", ["read_user"], "yokuha"),
    "no-scope": (APP_ID, ["openid"], "yokuha"),
    "stranger": (APP_ID, ["read_user"], "mallory"),
}
gitlab_calls = []


class FakeGitLab(BaseHTTPRequestHandler):
    def do_GET(self):
        token = self.headers.get("Authorization", "").removeprefix("Bearer ")
        gitlab_calls.append((self.path, token))
        if token not in GITLAB_TOKENS:
            self.send_response(401)
            self.end_headers()
            return
        uid, scopes, user = GITLAB_TOKENS[token]
        if self.path == "/oauth/token/info":
            body = {"resource_owner_id": 1, "scope": scopes, "expires_in": 7200, "application": {"uid": uid}}
        elif self.path == "/api/v4/user":
            body = {"id": 1, "username": user}
        else:
            self.send_response(404)
            self.end_headers()
            return
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def sh(*cmd, cwd=None):
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def tool(client, name, **args):
    res = await client.call_tool(name, args)
    text = "".join(c.text for c in res.content if hasattr(c, "text"))
    return res.is_error, text


def check(label, cond, detail=""):
    print(("OK   " if cond else "FAIL ") + label + (f"  -> {detail}" if not cond else ""))
    if not cond:
        raise SystemExit(1)


async def main():
    tmp = Path(tempfile.mkdtemp())
    remote, seed = tmp / "remote.git", tmp / "seed"
    sh("git", "init", "--bare", "-b", "main", str(remote))
    sh("git", "clone", str(remote), str(seed))
    sh("git", "config", "user.email", "t@t", cwd=seed)
    sh("git", "config", "user.name", "T", cwd=seed)
    (seed / "README.md").write_text("# Readme\nLies alle Dateien.\n")
    (seed / "Allgemeine Anweisungen.md").write_text("* Du bist Claudi\n* Alter beachten: Jahrgang 1900, Recovery!\n")
    (seed / "Ziele.md").write_text("# Ziele\nTestziel Alpha\n")
    (seed / "Ziele.md.~1~").write_text("Emacs-Backup, darf nie auftauchen\n")
    (seed / ".gitignore").write_text("*~\n")
    sh("git", "add", ".", cwd=seed)
    sh("git", "commit", "-m", "init", cwd=seed)
    sh("git", "push", "origin", "HEAD:main", cwd=seed)

    gitlab = HTTPServer(("127.0.0.1", 0), FakeGitLab)
    threading.Thread(target=gitlab.serve_forever, daemon=True).start()
    gitlab_url = f"http://127.0.0.1:{gitlab.server_address[1]}"

    port = free_port()
    env = {
        **os.environ,
        "COACH_REPO_URL": str(remote),
        "COACH_REPO_DIR": str(tmp / "server-clone"),
        "COACH_URL_TOKEN": TOKEN,
        "COACH_WEBHOOK_TOKEN": HOOK,
        "COACH_WRITE": "1",
        "COACH_SYNC_TTL": "0",
        "COACH_PORT": str(port),
        "COACH_ALLOWED_HOSTS": f"127.0.0.1:{port},localhost:{port}",
        "COACH_GIT_EMAIL": "claudi@test",
        "COACH_OAUTH_ISSUER": gitlab_url,
        "COACH_OAUTH_CLIENT_ID": APP_ID,
        "COACH_ALLOWED_USERS": "yokuha",
        "COACH_PUBLIC_URL": f"http://127.0.0.1:{port}",
    }
    proc = subprocess.Popen([sys.executable, str(HERE / "coach_mcp.py")], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1)
                break
            except Exception:
                time.sleep(0.2)
        else:
            raise SystemExit("Server does not start:\n" + proc.stdout.read())

        base = f"http://127.0.0.1:{port}"
        for bad in (f"{base}/falsch/mcp", f"{base}/{TOKEN[:-1]}/mcp", f"{base}/ä/mcp"):
            try:
                urllib.request.urlopen(urllib.request.Request(bad.replace("ä", "%C3%A4"), method="POST"))
                check("wrong path is rejected", False)
            except urllib.error.HTTPError as e:
                check(f"wrong path {bad[len(base):]} -> 404", e.code == 404)

        # OAuth: metadata, challenge, token checks
        meta = json.load(urllib.request.urlopen(f"{base}/.well-known/oauth-protected-resource/mcp"))
        check("resource metadata points to GitLab", meta["authorization_servers"] == [gitlab_url]
              and meta["resource"] == f"{base}/mcp", meta)
        for token, status in (("", 401), ("unknown", 401), ("other-app", 401), ("no-scope", 403), ("stranger", 403)):
            req = urllib.request.Request(f"{base}/mcp", method="POST", data=b"{}",
                                         headers={"Authorization": f"Bearer {token}"} if token else {})
            try:
                urllib.request.urlopen(req)
                check(f"token {token!r} rejected", False)
            except urllib.error.HTTPError as e:
                challenge = e.headers.get("WWW-Authenticate", "")
                check(f"token {token or '(none)'!r} -> {status} with challenge",
                      e.code == status and "resource_metadata=" in challenge, (e.code, challenge))

        oauth_http = httpx2.AsyncClient(headers={"Authorization": "Bearer good"})
        async with Client(streamable_http_client(f"{base}/mcp", http_client=oauth_http)) as c:
            check("OAuth: all 8 tools present", len((await c.list_tools()).tools) == 8)
            err, text = await tool(c, "get_instructions")
            check("OAuth: get_instructions works", not err and "Jahrgang 1900" in text, text[:300])
        info_calls = [path for path, tok in gitlab_calls if tok == "good" and path == "/oauth/token/info"]
        check("OAuth: token check is cached", len(info_calls) == 1, gitlab_calls)

        async with Client(f"{base}/{TOKEN}/mcp") as c:
            tools = {t.name for t in (await c.list_tools()).tools}
            check("all 8 tools present", len(tools) == 8, tools)

            err, text = await tool(c, "get_instructions")
            check("get_instructions returns README first", text.index("README.md") < text.index("Allgemeine"), text[:300])
            check("Emacs backup not included", "Emacs-Backup" not in text)
            check("umlauts/content ok", "Jahrgang 1900" in text and "init" in text)

            err, text = await tool(c, "search_instructions", query="testziel")
            check("search finds Ziele.md", "Ziele.md:2" in text, text)

            err, text = await tool(c, "read_instruction_file", path="../etc/passwd")
            check("path trick rejected", err, text)
            err, text = await tool(c, "read_instruction_file", path="Ziele.md.~1~")
            check("backup file not readable", err, text)

            # external change (e.g. git push from the Mac) arrives
            (seed / "Ziele.md").write_text("# Ziele\nTestziel Alpha\nNeu von aussen\n")
            sh("git", "commit", "-am", "extern", cwd=seed)
            sh("git", "push", "origin", "HEAD:main", cwd=seed)
            err, text = await tool(c, "read_instruction_file", path="Ziele.md")
            check("external push visible", "Neu von aussen" in text, text)

            # Webhook
            req = urllib.request.Request(f"{base}/hook/gitlab", method="POST", headers={"X-Gitlab-Token": HOOK})
            check("Webhook 202", urllib.request.urlopen(req).status == 202)
            req = urllib.request.Request(f"{base}/hook/gitlab", method="POST", headers={"X-Gitlab-Token": "x"})
            try:
                urllib.request.urlopen(req)
                check("webhook with wrong token rejected", False)
            except urllib.error.HTTPError as e:
                check("webhook with wrong token -> 404", e.code == 404)

            # writing: replace
            err, text = await tool(c, "replace_in_instruction_file", path="Ziele.md",
                                   old="Neu von aussen", new="Von Claudi geaendert", commit_message="Ziele praezisiert")
            check("replace commits and pushes", not err and "Committet" in text, text)
            err, text = await tool(c, "replace_in_instruction_file", path="Ziele.md",
                                   old="gibt es nicht", new="x", commit_message="x")
            check("replace without match fails", err, text)
            err, text = await tool(c, "write_instruction_file", path="neu/Training.md",
                                   content="# Neu\nÄnderung mit Umlauten ß\n", commit_message="Neue Datei")
            check("write new file in subfolder", not err and "Committet" in text, text)
            err, text = await tool(c, "write_instruction_file", path="skript.sh", content="rm -rf", commit_message="x")
            check("wrong extension rejected", err, text)
            err, text = await tool(c, "write_instruction_file", path=".git/config", content="x", commit_message="x")
            check(".git rejected", err, text)

            sh("git", "pull", "--rebase", "origin", "main", cwd=seed)
            check("remote has Claudi's change", "Von Claudi geaendert" in (seed / "Ziele.md").read_text())
            log = subprocess.run(["git", "log", "--format=%s", "-3"], cwd=seed, capture_output=True, text=True).stdout
            check("commit message with (by Claude)", "Neue Datei (by Claude)" in log, log)

            # conflict: seed changes the same line, the server must fail cleanly
            (seed / "Ziele.md").write_text("# Ziele\nKonflikt-Variante\n")
            sh("git", "commit", "-am", "konflikt", cwd=seed)
            sh("git", "push", "origin", "HEAD:main", cwd=seed)
            err, text = await tool(c, "replace_in_instruction_file", path="Ziele.md",
                                   old="Von Claudi geaendert", new="egal", commit_message="x")
            check("stale text -> clean error instead of data loss", err, text)

            err, text = await tool(c, "instructions_history", path="Ziele.md", limit=3)
            check("History", "konflikt" in text, text)

        proc.terminate()
        out = proc.communicate(timeout=5)[0]
        check("request log names the tool", "tools/call get_instructions -> 200" in out, out[-2000:])
        check("request log has no secret path", TOKEN not in out)
        check("request log names the OAuth user", "user: yokuha" in out, out[-2000:])
        check("request log has no OAuth token", "Bearer good" not in out)
        check("request log has no arguments", "Von Claudi geaendert" not in out)
        print("\nALL TESTS PASSED")
    finally:
        proc.terminate()
        proc.wait(5)
        gitlab.shutdown()


asyncio.run(main())
