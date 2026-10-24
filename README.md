# coach-instructions-mcp

MCP-Server, der die Anweisungen für Coach Claudi aus einem GitLab-Repository bereitstellt.
Der Server hält auf dem VPS einen lokalen Clone aktuell; Claudi fragt ihn per Connector ab
(Browser, iPhone, Desktop) und kann auf Wunsch Änderungen committen.

```
Mac / Emacs ──git push──▶ gitlab.com ──Webhook──▶ VPS: coach-mcp ◀──MCP── Claude (Claudi)
                              ▲                      │ lokaler Clone
                              └─────git push (nur wenn COACH_WRITE=1)─┘
```

## Tools

| Tool                                                    | Zweck                                                                       |
|---------------------------------------------------------|-----------------------------------------------------------------------------|
| `get_instructions`                                      | Alle `.md`/`.txt`-Dateien (README zuerst) in einem Rutsch, mit Commit-Stand |
| `list_instruction_files`, `read_instruction_file`       | Einzeldateien                                                               |
| `search_instructions`                                   | Volltextsuche (optional Regex)                                              |
| `instructions_history`                                  | `git log`, optional pro Datei                                               |
| `sync_instructions`                                     | sofortiger Abgleich mit GitLab                                              |
| `write_instruction_file`, `replace_in_instruction_file` | nur mit `COACH_WRITE=1`; committet, hängt „(by Claude)“ an und pusht        |

Nur tracked Dateien werden ausgeliefert, Emacs-Backups (`*.~1~`) bleiben also außen vor,
solange sie nicht eingecheckt sind (siehe `.gitignore` unten).

## 1. Repository auf GitLab anlegen

Auf dem Mac, im Ordner `Coach Claudi`:

```sh
cd /Ordner/mit/den/Anweisungen
printf '*~\n.#*\n.dir-locals.el\n' > .gitignore      # Backups raus; .dir-locals.el nach Wunsch behalten
git init -b main
git add .
git commit -m "Coach-Claudi-Anweisungen"
# Projekt auf gitlab.com anlegen (privat!), dann:
git remote add origin git@gitlab.com:<group>/<project>.git
git push -u origin main
```

Das Repository sollte **privat** sein (stehen persönliche Daten drin).

## 2. Deploy Key

Auf dem VPS:

```sh
sudo useradd --system --home /var/lib/coach-mcp --shell /usr/sbin/nologin coach-mcp
sudo install -d -o coach-mcp -g coach-mcp -m 750 /var/lib/coach-mcp
sudo -u coach-mcp ssh-keygen -t ed25519 -N '' -C coach-mcp -f /var/lib/coach-mcp/deploy_key
sudo cat /var/lib/coach-mcp/deploy_key.pub
```

In GitLab: *Projekt → Settings → Repository → Deploy keys* → Public Key einfügen.
Schreibrecht („Grant write permissions“) nur aktivieren, wenn Claudi Änderungen pushen soll.
Ist `main` geschützt, den Key unter *Protected branches → Allowed to push* freigeben.

## 3. Installation auf dem VPS

```sh
sudo git clone https://github.com/yokuha/coach-instruction-mcp.git /opt/coach-mcp/src
cd /opt/coach-mcp/src
sudo python3 -m venv /opt/coach-mcp/venv
sudo /opt/coach-mcp/venv/bin/pip install .

sudo cp deploy/coach-mcp.env.example /etc/coach-mcp.env
sudo chmod 600 /etc/coach-mcp.env
sudo nano /etc/coach-mcp.env          # Repo-URL, zwei Tokens (openssl rand -hex 24), Host eintragen

sudo cp deploy/coach-mcp.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now coach-mcp
journalctl -u coach-mcp -f            # "Erster Abgleich: ok" erwarten
curl -s http://127.0.0.1:8765/healthz
```

Reverse Proxy und DNS: siehe `deploy/reverse-proxy.md`.

Update auf eine neue Version:

```sh
cd /opt/coach-mcp/src && sudo git pull
sudo /opt/coach-mcp/venv/bin/pip install .
sudo systemctl restart coach-mcp
```

## 4. GitLab-Webhook (sofortige Aktualisierung)

*Projekt → Settings → Webhooks*: URL `https://coach-mcp.domain.tld/hook/gitlab`,
Secret token = `COACH_WEBHOOK_TOKEN`, Trigger „Push events“ (Branch `main`), SSL-Prüfung an.
Ohne Webhook greifen trotzdem der Abgleich bei Zugriff (höchstens alle 20 s) und der
Hintergrund-Abgleich (alle 5 min).

## 5. Connector in Claude

*Einstellungen → Connectors → Benutzerdefinierten Connector hinzufügen*, URL:

```
https://coach-mcp.hostname.tld/<COACH_URL_TOKEN>/mcp
```

Kein OAuth eintragen. Danach in den Projekt-Anweisungen den Abschnitt zum Lesen des lokalen
Ordners ersetzen, z. B.:

> In jedem neuen Chat und vor relevanten Planungen, Änderungen oder Auswertungen rufst du
> `get_instructions` vom Connector „Coach Claudi“ auf, wertest die Anweisungen aus und
> berücksichtigst sie genauso wie diese Instruktionen. Änderungen an den Anweisungen schreibst
> du nur nach meiner ausdrücklichen Zustimmung.

## Sicherheit

* Authentifizierung ist ein geheimer URL-Pfad (≥ 24 Zeichen, konstantzeitiger Vergleich),
  denn claude.ai-Connectoren können keinen eigenen Bearer-Header senden. Der Pfad steht bei
  Anthropic in deiner Connector-Konfiguration und im Reverse-Proxy-Log (Access-Log des Proxys
  für diesen Host am besten abschalten; der Server selbst loggt keine Pfade). Bei Verdacht:
  Token in `/etc/coach-mcp.env` wechseln, Dienst neu starten, Connector-URL anpassen.
  Wer mehr will, braucht OAuth; das lässt sich später nachrüsten (`auth=` am `MCPServer`).
* `COACH_ALLOWED_HOSTS` schützt gegen DNS-Rebinding.
* Schreiben ist standardmäßig **aus**. Mit `COACH_WRITE=1` kann Claudi nur `.md`/`.txt`
  im Repository ändern (kein `.git`, keine Pfad-Tricks, max. 200 kB), jede Änderung ist ein
  Commit mit „(by Claude)“ und über `git revert` in GitLab rückgängig zu machen.
  Hinweis: Wenn Claudi Text aus anderen Quellen verarbeitet (Webseiten, Notizen), könnte
  darin versteckte Anweisung versuchen, die Coach-Anweisungen umzuschreiben. Deshalb der
  Hinweis in den Tool-Beschreibungen „nur nach ausdrücklicher Zustimmung“; im Zweifel
  Branch-Schutz plus Merge Request statt direktem Push nutzen (lässt sich nachrüsten).
* Ein fehlgeschlagener Abgleich (GitLab nicht erreichbar) lässt den letzten Stand weiterlaufen;
  der Fehler steht im Kopf jeder Antwort. Schreib-Tools brechen in dem Fall ab.

## Test ohne GitLab

```sh
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/python test_local.py
```

Der Test baut ein lokales Bare-Repo als Remote und prüft Lesen, Suche, externe Pushes,
Webhook, Schreiben, Konflikte und Pfad-Schutz.
