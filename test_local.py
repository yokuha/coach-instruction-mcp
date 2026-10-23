"""Lokaler End-to-End-Test ohne GitLab: nutzt ein lokales Bare-Repo als "Remote".

Aufruf:  python test_local.py
"""

import asyncio
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from mcp import Client

HERE = Path(__file__).parent
TOKEN = "t" * 32
HOOK = "hooksecret"


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
            raise SystemExit("Server startet nicht:\n" + proc.stdout.read())

        base = f"http://127.0.0.1:{port}"
        for bad in (f"{base}/falsch/mcp", f"{base}/mcp"):
            try:
                urllib.request.urlopen(bad)
                check("falscher Pfad wird abgelehnt", False)
            except urllib.error.HTTPError as e:
                check(f"falscher Pfad {bad[len(base):]} -> 404", e.code == 404)

        async with Client(f"{base}/{TOKEN}/mcp") as c:
            tools = {t.name for t in (await c.list_tools()).tools}
            check("alle 8 Tools vorhanden", len(tools) == 8, tools)

            err, text = await tool(c, "get_instructions")
            check("get_instructions liefert README zuerst", text.index("README.md") < text.index("Allgemeine"), text[:300])
            check("Emacs-Backup nicht enthalten", "Emacs-Backup" not in text)
            check("Umlaute/Inhalt ok", "Jahrgang 1900" in text and "init" in text)

            err, text = await tool(c, "search_instructions", query="testziel")
            check("Suche findet Ziele.md", "Ziele.md:2" in text, text)

            err, text = await tool(c, "read_instruction_file", path="../etc/passwd")
            check("Pfad-Trick abgelehnt", err, text)
            err, text = await tool(c, "read_instruction_file", path="Ziele.md.~1~")
            check("Backup-Datei nicht lesbar", err, text)

            # Aenderung von aussen (z. B. per git push vom Mac) kommt an
            (seed / "Ziele.md").write_text("# Ziele\nTestziel Alpha\nNeu von aussen\n")
            sh("git", "commit", "-am", "extern", cwd=seed)
            sh("git", "push", "origin", "HEAD:main", cwd=seed)
            err, text = await tool(c, "read_instruction_file", path="Ziele.md")
            check("externer Push sichtbar", "Neu von aussen" in text, text)

            # Webhook
            req = urllib.request.Request(f"{base}/hook/gitlab", method="POST", headers={"X-Gitlab-Token": HOOK})
            check("Webhook 202", urllib.request.urlopen(req).status == 202)
            req = urllib.request.Request(f"{base}/hook/gitlab", method="POST", headers={"X-Gitlab-Token": "x"})
            try:
                urllib.request.urlopen(req)
                check("Webhook mit falschem Token abgelehnt", False)
            except urllib.error.HTTPError as e:
                check("Webhook mit falschem Token -> 404", e.code == 404)

            # Schreiben: replace
            err, text = await tool(c, "replace_in_instruction_file", path="Ziele.md",
                                   old="Neu von aussen", new="Von Claudi geaendert", commit_message="Ziele praezisiert")
            check("replace committet und pusht", not err and "Committet" in text, text)
            err, text = await tool(c, "replace_in_instruction_file", path="Ziele.md",
                                   old="gibt es nicht", new="x", commit_message="x")
            check("replace ohne Treffer scheitert", err, text)
            err, text = await tool(c, "write_instruction_file", path="neu/Training.md",
                                   content="# Neu\nÄnderung mit Umlauten ß\n", commit_message="Neue Datei")
            check("write neue Datei in Unterordner", not err and "Committet" in text, text)
            err, text = await tool(c, "write_instruction_file", path="skript.sh", content="rm -rf", commit_message="x")
            check("falsche Endung abgelehnt", err, text)
            err, text = await tool(c, "write_instruction_file", path=".git/config", content="x", commit_message="x")
            check(".git abgelehnt", err, text)

            sh("git", "pull", "--rebase", "origin", "main", cwd=seed)
            check("Remote hat Claudi-Aenderung", "Von Claudi geaendert" in (seed / "Ziele.md").read_text())
            log = subprocess.run(["git", "log", "--format=%s", "-3"], cwd=seed, capture_output=True, text=True).stdout
            check("Commit-Nachricht mit (by Claude)", "Neue Datei (by Claude)" in log, log)

            # Konflikt: seed aendert dieselbe Zeile, Server soll sauber scheitern
            (seed / "Ziele.md").write_text("# Ziele\nKonflikt-Variante\n")
            sh("git", "commit", "-am", "konflikt", cwd=seed)
            sh("git", "push", "origin", "HEAD:main", cwd=seed)
            err, text = await tool(c, "replace_in_instruction_file", path="Ziele.md",
                                   old="Von Claudi geaendert", new="egal", commit_message="x")
            check("veralteter Text -> sauberer Fehler statt Datenverlust", err, text)

            err, text = await tool(c, "instructions_history", path="Ziele.md", limit=3)
            check("History", "konflikt" in text, text)
        print("\nALLE TESTS BESTANDEN")
    finally:
        proc.terminate()
        proc.wait(5)


asyncio.run(main())
