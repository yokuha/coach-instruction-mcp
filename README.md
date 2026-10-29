# coach-instructions-mcp

An MCP server that serves the instructions for your AI coach ("Coach Claudi") from a Git
repository. It runs on a small server (VPS or similar), keeps a local clone up to date, and
Claude reads it through a custom connector — in the browser, on the iPhone and in the desktop
app alike. Optionally, Claude can commit changes to the instructions.

```
Mac / editor ──git push──▶ Git host ──webhook──▶ VPS: coach-mcp ◀──MCP── Claude
                              ▲                    │ local clone
                              └──── git push (only with COACH_WRITE=1) ┘
```

## What you need

* A **private** Git repository with your instructions as `.md`/`.txt` files
  (GitLab, GitHub or any host reachable via SSH)
* A Linux server with Python ≥ 3.10, `git`, systemd and a public hostname
* A reverse proxy with TLS (Caddy or nginx), e.g. `coach-mcp.example.com`

Setup takes about 30 minutes: steps 1–5 below.

## Tools

| Tool                                                    | Purpose                                                                      |
|---------------------------------------------------------|------------------------------------------------------------------------------|
| `get_instructions`                                      | All `.md`/`.txt` files (README first) in one go, with the current commit     |
| `list_instruction_files`, `read_instruction_file`       | Single files                                                                 |
| `search_instructions`                                   | Full-text search (optionally regex)                                          |
| `instructions_history`                                  | `git log`, optionally per file                                               |
| `sync_instructions`                                     | Sync with the remote right now                                               |
| `write_instruction_file`, `replace_in_instruction_file` | Only with `COACH_WRITE=1`; commits with "(by Claude)" appended and pushes    |

Only tracked files are served, so editor backups stay out as long as they are not committed.
The tool descriptions Claude sees are currently in German and refer to "Coach Claudi".

## 1. Instructions repository

In the folder with your instructions:

```sh
printf '*~\n.#*\n' > .gitignore      # keep editor backups out
git init -b main
git add .
git commit -m "Coach instructions"
# create a PRIVATE project on your Git host, then:
git remote add origin git@gitlab.com:<group>/<project>.git
git push -u origin main
```

Keep it **private** — coaching instructions usually contain personal data.

## 2. Service user and deploy key

On the server:

```sh
sudo useradd --system --home /var/lib/coach-mcp --shell /usr/sbin/nologin coach-mcp
sudo install -d -o coach-mcp -g coach-mcp -m 750 /var/lib/coach-mcp
sudo -u coach-mcp ssh-keygen -t ed25519 -N '' -C coach-mcp -f /var/lib/coach-mcp/deploy_key
sudo cat /var/lib/coach-mcp/deploy_key.pub
```

Add the public key as a deploy key to the instructions repository:

* **GitLab:** *Settings → Repository → Deploy keys*. If `main` is protected and Claude should
  write, allow the key under *Protected branches → Allowed to push*.
* **GitHub:** *Settings → Deploy keys*.

Grant write access only if Claude should be able to push changes (`COACH_WRITE=1`).

## 3. Install

```sh
VERSION=v0.1.0
RAW=https://raw.githubusercontent.com/yokuha/coach-instruction-mcp/$VERSION/deploy

sudo python3 -m venv /opt/coach-mcp/venv
sudo /opt/coach-mcp/venv/bin/pip install "git+https://github.com/yokuha/coach-instruction-mcp@$VERSION"

sudo curl -fsSL -o /etc/coach-mcp.env "$RAW/coach-mcp.env.example"
sudo chmod 600 /etc/coach-mcp.env
sudo nano /etc/coach-mcp.env          # repo URL, two tokens, hostname (see comments in the file)

sudo curl -fsSL -o /etc/systemd/system/coach-mcp.service "$RAW/coach-mcp.service"
sudo systemctl daemon-reload
sudo systemctl enable --now coach-mcp
journalctl -u coach-mcp -f            # expect "Initial sync: ok"
curl -s http://127.0.0.1:8765/healthz
```

Generate each token with `openssl rand -hex 24`.

Then add the hostname to your reverse proxy and DNS: see
[deploy/reverse-proxy.md](deploy/reverse-proxy.md).

**Updating** to a newer release:

```sh
sudo /opt/coach-mcp/venv/bin/pip install --upgrade "git+https://github.com/yokuha/coach-instruction-mcp@v0.1.0"
sudo systemctl restart coach-mcp
```

(Replace `v0.1.0` with the release you want; see
[Releases](https://github.com/yokuha/coach-instruction-mcp/releases).)

Coming from v0.1.0: the command was renamed from `coach-mcp` to `coach-instruction-mcp`.
Fetch the new `coach-mcp.service` (or change `ExecStart` accordingly) and run
`sudo systemctl daemon-reload` before restarting.

## 4. Webhook for instant updates (optional, GitLab)

*Settings → Webhooks*: URL `https://coach-mcp.example.com/hook/gitlab`,
secret token = `COACH_WEBHOOK_TOKEN`, trigger "Push events" (branch `main`), SSL verification on.

Without a webhook (or on other hosts such as GitHub) the server still syncs on access (at most
every 20 s) and in the background (every 5 min), so changes arrive with a short delay.

## 5. Connector in Claude

*Settings → Connectors → Add custom connector*, URL:

```
https://coach-mcp.example.com/<COACH_URL_TOKEN>/mcp
```

Leave OAuth empty. Then tell Claude in your project instructions to use it, for example:

> At the start of every new chat and before relevant planning, changes or analyses, call
> `get_instructions` from the "Coach Claudi" connector and follow those instructions just like
> these. Only change the instructions after my explicit approval.

## Security

* Authentication is a secret URL path (≥ 24 characters, constant-time comparison), because
  claude.ai connectors cannot send a custom bearer header. The path is stored in your connector
  configuration at Anthropic and appears in reverse proxy logs — best disable the proxy's access
  log for this host (the server itself does not log paths). If in doubt: change the token in
  `/etc/coach-mcp.env`, restart the service and update the connector URL. OAuth could be added
  later (`auth=` on `MCPServer`).
* `COACH_ALLOWED_HOSTS` protects against DNS rebinding.
* Writing is **off** by default. With `COACH_WRITE=1` Claude can only change `.md`/`.txt` files
  in the repository (no `.git`, no path tricks, max. 200 kB). Every change is a commit marked
  "(by Claude)" and can be undone with `git revert`.
  Note: if Claude processes text from other sources (web pages, notes), hidden instructions in
  it could try to rewrite your coach instructions. That is why the tool descriptions say "only
  after explicit approval"; if in doubt, protect the branch and use merge requests instead of
  direct pushes.
* If a sync fails (Git host unreachable), the last state keeps being served and the error is
  shown at the top of every response. Write tools refuse to run in that case.

## Local test

```sh
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/python test_local.py
```

The test uses a local bare repository as the remote and checks reading, search, external
pushes, webhook, writing, conflicts and path protection.
