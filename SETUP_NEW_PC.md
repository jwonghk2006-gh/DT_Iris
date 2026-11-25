# Setting up DT_Iris on another Windows PC

The code is shared through a **private** GitHub repository. Keys, trades and downloaded
data are NOT in GitHub (see `.gitignore`): each PC has its own `.env` and its own data.

## 1. Install once

1. **Python 3.11** from python.org (64-bit). In the installer tick *"Add python.exe to PATH"*.
2. **Git for Windows** from git-scm.com (all default options).
3. A **GitHub account**, and an invitation to the private repository (the owner adds you
   under the repository's *Settings -> Collaborators*). Accept it from the email.

## 2. Get the code

Open **Windows PowerShell** and run one line at a time (PowerShell 5.1 has no `&&`):

```
cd $HOME\Documents
git clone https://github.com/jwonghk2006-gh/DT_Iris.git
cd DT_Iris
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe test_dt.py
```

The last line should end with `31/31 passed`. The first `git clone` opens a browser window
to sign in to GitHub.

## 3. Keys (`.env`)

```
copy .env.example .env
notepad .env
```

Fill in your OWN keys (do not copy someone else's `.env`; never send it by chat or email):

| Keys | Where | Needed for |
|---|---|---|
| `LONGPORT_APP_KEY`, `LONGPORT_APP_SECRET`, `LONGPORT_PAPER_ACCESS_TOKEN` | open.longbridge.com -> Developer (needs a Longbridge account with US market data) | scanner, runner, live quotes |
| `DT_TELEGRAM_BOT_TOKEN`, `DT_TELEGRAM_CHAT_ID` | BotFather, or ask to be added to the group "DT_Iris Notification" | alerts (optional) |
| `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` | alpaca.markets, free account -> API keys | 5-year research data (optional) |

Leave `LONGPORT_LIVE_ACCESS_TOKEN` and `DT_LIVE_ENABLED` empty.

Only ONE PC should run the scheduled scanner and post to the shared Telegram group,
or the group gets every message twice.

## 4. First run

```
.venv\Scripts\python.exe dt_telegram.py "hello from the new PC"
.venv\Scripts\python.exe dt_scanner.py
run_dashboard.bat
```

The first scan builds the stock list from Longbridge (`data\universe_all.csv`), so it takes
a minute longer. The dashboard opens at `http://localhost:8518`.

## 5. Research data (optional, about 2-4 hours)

```
.venv\Scripts\python.exe dt_alpaca.py --check
.venv\Scripts\python.exe dt_alpaca.py --years 5
```

This rebuilds `data_alpaca\`. Its holdout date is set when it is built; write it down in
`docs\RESEARCH_LOG.md`.

## 6. Getting updates later

```
cd $HOME\Documents\DT_Iris
git pull
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe test_dt.py
```

If you changed files yourself, `git pull` may complain: talk to the other person before
overwriting anything. Daily use is in `USER_MANUAL.md`; the research history is in
`docs\RESEARCH_LOG.md`; to continue with an AI assistant, use `docs\HANDOVER_PROMPT.md`.
