# AlertBot

A Lark bot that watches the **MonitorFlow** dashboard (`monitor.client8.me`) and:

1. **Detects new alerts** in real time and pushes them to a Lark chat — with the
   alert **title + content** and a **screenshot of the alert's detail (👁) window**.
2. Answers the **`/check`** command (when you @-mention the bot): it **reacts**
   with a "processing" emoji while working, then swaps it for a "done" emoji, and
   replies with a **card split into two categories** — alerts **still firing** and
   alerts **resolved**.
3. Connects to Lark through a **WebSocket long connection** (event subscription
   mode = *persistent connection*) — no public webhook URL required.

---

## How it works

```
                 ┌─────────────────────────────┐
   poll (60s)    │        watcher thread        │  new alert → screenshot (Playwright)
  ┌─────────────►│  new-alert + resolve detect  │───────────────► upload image ─┐
  │              └─────────────────────────────┘                                │
MonitorFlow API                                                          Lark REST API
(/altermanager/api)                                                       (send card)
  ▲              ┌─────────────────────────────┐
  │  /check      │   Lark WebSocket (long conn) │  @bot /check → react ⏳ → reply card → react ✅
  └──────────────│   im.message.receive_v1      │
                 └─────────────────────────────┘
```

Files:

| File | Purpose |
|------|---------|
| `main.py` | Entrypoint: starts watcher + WebSocket, routes `/check` and `/chatid`. |
| `config.py` | Loads `.env`. |
| `monitor_client.py` | MonitorFlow API (login, list/get alerts, stats). |
| `lark_client.py` | Lark REST wrapper (reactions, reply, send card, image upload). |
| `watcher.py` | Background polling loop. |
| `commands.py` | `/check` handler. |
| `cards.py` | Lark card builders. |
| `screenshot.py` | Playwright capture of the alert detail modal. |
| `state.py` | Persistent JSON state (so restarts don't re-announce). |
| `alertbot.service` | systemd unit. |

---

## 1. Lark app setup (one-time, in the Lark Developer Console)

Open <https://open.larksuite.com/app> → your app (the `cli_…` App ID from your `.env`) and configure:

1. **Bot** → enable the bot capability ("Add features → Bot").
2. **Permissions / Scopes** — add and publish:
   - `im:message` (receive & send messages)
   - `im:message:send_as_bot`
   - `im:message.reaction:write` (add/remove emoji reactions)
   - `im:resource` (upload images)
3. **Event Subscription**:
   - Set subscription mode to **"Use long connection to receive events"**
     (持久连接 / long connection). *No request URL is needed in this mode.*
   - Subscribe to the event **`im.message.receive_v1`** (接收消息).
4. **Publish a version** and wait for admin approval, otherwise scopes/events
   won't take effect.
5. Add the bot to the group(s) where you'll use `/check`.

> The **Verification Token** is only used by webhook mode. In long-connection
> mode the WebSocket authenticates with the App ID + App Secret. It's kept in
> `.env` for completeness / in case you ever switch modes.

---

## 2. Local development (Windows / macOS / Linux)

```bash
# clone (or open the existing folder)
git clone https://github.com/mrcodestealer/alertbot.git
cd alertbot

# virtualenv
python -m venv .venv
# Windows PowerShell:
.venv\Scripts\Activate.ps1
# macOS/Linux:
source .venv/bin/activate

pip install -r requirements.txt
playwright install chromium          # downloads the headless browser

cp .env.example .env                 # then edit .env  (Windows: copy .env.example .env)
python main.py
```

Send `/chatid` to the bot in your group → it replies with the `chat_id`. Put that
value in `LARK_ALERT_CHAT_ID` in `.env` and restart, so proactive alert pushes
land in that chat.

---

## 3. Deploy to a server + systemd service

```bash
# on the server, as root/sudo
sudo useradd --system --create-home --home-dir /home/alertbot alertbot
sudo mkdir -p /opt/alertbot
sudo chown alertbot:alertbot /opt/alertbot

# get the code (see the git section for auth options)
sudo -u alertbot git clone https://github.com/mrcodestealer/alertbot.git /opt/alertbot
cd /opt/alertbot

# python env
sudo -u alertbot python3 -m venv .venv
sudo -u alertbot /opt/alertbot/.venv/bin/pip install -r requirements.txt

# install the headless browser + its OS libraries (Playwright)
sudo -u alertbot HOME=/home/alertbot /opt/alertbot/.venv/bin/playwright install chromium
sudo /opt/alertbot/.venv/bin/playwright install-deps   # apt packages Chromium needs

# create the .env (paste the one provided) and lock it down
sudo -u alertbot nano /opt/alertbot/.env
sudo chmod 600 /opt/alertbot/.env
```

Install and start the service:

```bash
sudo cp /opt/alertbot/alertbot.service /etc/systemd/system/alertbot.service
# edit User/paths in the unit if you didn't use alertbot:/opt/alertbot
sudo systemctl daemon-reload
sudo systemctl enable --now alertbot

# operate it
sudo systemctl status alertbot
sudo systemctl restart alertbot
sudo systemctl stop alertbot
journalctl -u alertbot -f            # live logs
```

If `ENABLE_SCREENSHOT=true` and Chromium can't launch on a minimal server, run
`playwright install-deps` (installs the required apt libraries), or set
`ENABLE_SCREENSHOT=false` to disable screenshots (alerts still push as text cards).

---

## 4. Git: pull/push on both local and server

The repo is <https://github.com/mrcodestealer/alertbot>.

### First push from your local machine (repo currently empty)

```bash
cd C:\Users\jcsia\Desktop\ALL\AlertBot        # your local folder
git init
git branch -M main
git remote add origin https://github.com/mrcodestealer/alertbot.git
git add .
git commit -m "Initial AlertBot"
git push -u origin main
```

`.env`, `state.json`, `screenshots/` are in `.gitignore`, so **secrets are never
pushed**. The server keeps its own `.env`.

### Authentication (pick one)

- **HTTPS + Personal Access Token (simplest):** create a PAT at
  GitHub → Settings → Developer settings → *Fine-grained tokens* with
  `Contents: Read/Write` on this repo. When git asks for a password, paste the
  PAT. Cache it so you're not asked every time:
  ```bash
  git config --global credential.helper store   # server: stores in ~/.git-credentials
  # Windows local: git config --global credential.helper manager
  ```
- **SSH (recommended for the server):**
  ```bash
  sudo -u alertbot ssh-keygen -t ed25519 -C "alertbot-server"     # press enter through prompts
  sudo -u alertbot cat /home/alertbot/.ssh/id_ed25519.pub          # add this to GitHub → Deploy keys (Allow write)
  # then use the SSH remote:
  sudo -u alertbot git -C /opt/alertbot remote set-url origin git@github.com:mrcodestealer/alertbot.git
  ```

### Daily workflow

**Local — make changes and push:**
```bash
git pull                          # get latest first
# ...edit code...
git add -A
git commit -m "what changed"
git push
```

**Server — pull the update and restart:**
```bash
cd /opt/alertbot
sudo -u alertbot git pull
sudo -u alertbot /opt/alertbot/.venv/bin/pip install -r requirements.txt   # only if deps changed
sudo systemctl restart alertbot
```

> Edit code **locally**, push, then `git pull` on the server. Avoid editing on the
> server; if you must, commit/push from the server the same way. If `git pull` ever
> complains about the ignored `.env`/`state.json`, it won't — they're gitignored and
> stay untouched by pulls.

One-liner you can rerun on the server to deploy the latest:
```bash
cd /opt/alertbot && sudo -u alertbot git pull && sudo systemctl restart alertbot && journalctl -u alertbot -f
```

---

## 5. Using the bot

- **New alerts:** appear automatically in `LARK_ALERT_CHAT_ID` as a red card with
  the title, severity, instance, description, and a screenshot of the detail window.
  When an alert recovers, a green "Resolved" card is sent (toggle with `NOTIFY_ON_RESOLVE`).
- **`/check`:** in the group, type `@AlertBot /check`. The bot reacts ⏳ (`OnIt`),
  builds the summary, removes ⏳, reacts ✅ (`DONE`), and replies with a card showing
  **🔥 Still Firing** and **✅ Resolved** categories. In a direct chat you can just
  send `/check`.
- **`/chatid`:** the bot replies with the current chat's `chat_id`.

---

## 6. Configuration reference

See `.env.example`. Key ones:

| Variable | Default | Meaning |
|----------|---------|---------|
| `WATCH_SEVERITY` | `critical` | Which severities to watch (`critical`/`warning`/`info`/`all`). |
| `POLL_INTERVAL_SECONDS` | `60` | How often to poll for new/resolved alerts. |
| `LARK_ALERT_CHAT_ID` | *(blank)* | Chat to push alerts to (blank = pushes disabled). |
| `ANNOUNCE_BACKLOG_ON_START` | `false` | Announce already-firing alerts on first start? |
| `NOTIFY_ON_RESOLVE` | `true` | Send a card when an alert recovers. |
| `ENABLE_SCREENSHOT` | `true` | Attach a Playwright screenshot of the detail modal. |
| `REACTION_PROCESSING` / `REACTION_DONE` | `OnIt` / `DONE` | Emoji reactions for `/check`. |

---

## 7. Troubleshooting

- **`pip install lark-oapi` fails on Windows with a long-path / `No such file or
  directory` error.** The SDK ships very deep file paths that exceed Windows'
  260-char limit. Either enable long paths once (admin PowerShell:
  `Set-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem' LongPathsEnabled 1`,
  then reboot), or create your project/venv at a short path like `C:\alertbot`.
  This does not affect the Linux server.
- **No new-alert cards appear.** Check `LARK_ALERT_CHAT_ID` is set (send `/chatid`
  to get it) and that `WATCH_SEVERITY` matches alerts that are actually firing.
  With `WATCH_SEVERITY=critical` the bot only reacts to CRITICAL alerts — if only
  WARNING/info are firing you'll see nothing. Set `WATCH_SEVERITY=all` (or
  `warning`) to widen it.
- **`/check` doesn't respond.** Make sure the bot is in the chat, you @-mentioned
  it (in groups), and the event `im.message.receive_v1` + long-connection mode are
  enabled and the app version is published.
- **Screenshots are blank / missing.** Run `playwright install chromium` and, on a
  server, `playwright install-deps`. Set `ENABLE_SCREENSHOT=false` to disable.

## 8. Security note

`.env` contains live secrets (Lark App Secret, dashboard password). It is
gitignored and should be `chmod 600` on the server. If a secret is ever committed
or leaked, **rotate it** (regenerate the App Secret in the Lark console, change the
dashboard password).
