# ⚡ Gridiron Fantasy

Snake draft · Pony system · 2× multipliers · Distance kickers · Multi-league

---

## Getting Started in VS Code

### Prerequisites

| Tool | Version | Download |
|------|---------|----------|
| Python | 3.11+ | [python.org](https://python.org) |
| VS Code | Latest | [code.visualstudio.com](https://code.visualstudio.com) |
| Git | Any | [git-scm.com](https://git-scm.com) |

---

### Step 1 — Open the project

```bash
cd gridiron          # wherever you cloned / unzipped the project
code .               # opens VS Code in this folder
```

When VS Code opens it will show a popup: **"Install recommended extensions?"** → click **Install All**.
These give you Python IntelliSense, the debugger, and Jinja template highlighting.

---

### Step 2 — Run setup (one time only)

Open the VS Code terminal (`Ctrl+\`` or **View → Terminal**) and run:

```bash
bash setup.sh
```

This creates a `.venv/` virtual environment and installs all packages. Takes about 30 seconds.

**Windows users** — if `bash` isn't available, run these manually in PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
mkdir -Force data
```

---

### Step 3 — Select the Python interpreter

1. Press `Ctrl+Shift+P` → type **"Python: Select Interpreter"**
2. Choose **`.venv/bin/python`** (or `.venv\Scripts\python` on Windows)

You'll see the interpreter shown in the bottom-left status bar. If it says the venv path, you're set.

---

### Step 4 — Press F5

That's it. The app starts with the debugger attached.

- App URL: **http://localhost:8000**
- Default login: `admin` / `admin123`  *(change `ADMIN_PASSWORD` in `.env`)*

The terminal shows the uvicorn logs. Any `.py` file you save triggers an automatic reload.
You can set breakpoints in `main.py` and they will be hit.

---

### Step 5 — Verify it works

Open **http://localhost:8000** and walk through this checklist:

- [ ] Log in as `admin`
- [ ] Create a league
- [ ] Register a second account in a private/incognito window
- [ ] Join the league with the invite code
- [ ] Both accounts create teams
- [ ] Add players via **Manage → Players**
- [ ] Run a test draft — timer should count down if configured
- [ ] Add ponies and set multipliers
- [ ] Enter scores via **Manage → Score Entry**
- [ ] Check the **Scores** page and **Audit Log**

---

## Project Structure

```
gridiron/
├── main.py            # FastAPI routes — the whole app lives here
├── db.py              # Database abstraction (SQLite dev / Postgres prod)
├── scoring.py         # Points calculation engine
├── stat_parsing.py    # ESPN stat JSON → scoring dict
├── requirements.txt   # Python dependencies
├── setup.sh           # One-time dev setup script
│
├── templates/         # Jinja2 HTML templates
│   ├── base.html          # Shared nav + layout
│   ├── dashboard.html     # League list
│   ├── draft.html         # Draft room (SSE live-refresh + timer)
│   ├── scores.html        # Weekly scores + standings
│   ├── manage.html        # Commissioner panel
│   └── audit_log.html     # Action history
│
├── tests/             # Unit test suite (50 tests)
│   ├── test_scoring_math.py
│   ├── test_multipliers.py
│   ├── test_kickers.py
│   └── test_stat_parsing.py
│
├── .vscode/
│   ├── launch.json    # F5 run config + test runner
│   ├── settings.json  # Python interpreter, formatter, file associations
│   └── extensions.json # Recommended extensions
│
├── .env               # Your local secrets (git-ignored)
├── .env.example       # Template — copy to .env
├── .gitignore
│
├── Dockerfile         # Multi-stage production build
├── docker-compose.yml # App + Postgres + Redis + Nginx
└── nginx/
    └── nginx.conf     # TLS termination + SSE no-buffer config
```

---

## Environment Variables

All configuration lives in `.env`. The important ones:

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | *(empty)* | Leave blank for SQLite. Set `postgresql://...` for Postgres. |
| `DB_PATH` | `data/fantasy.db` | SQLite file location |
| `JWT_SECRET` | *(dev default)* | **Change before deploying.** Signs all login tokens. |
| `JWT_EXPIRE_HOURS` | `72` | How long login sessions last |
| `SECURE_COOKIES` | `0` | Set to `1` in production (HTTPS only) |
| `ADMIN_PASSWORD` | *(prompted)* | Password for the `admin` superadmin account |

---

## Running Tests

```bash
# In the VS Code terminal (with .venv active)
python -m unittest discover -s tests -p "test_*.py" -v

# Or press F5 and select "Run Tests" from the dropdown
```

---

## Common Issues

**"ModuleNotFoundError: No module named 'jose'"**
→ The venv isn't active. Run `bash setup.sh` again, or manually: `source .venv/bin/activate`

**"Address already in use" on port 8000**
→ Another process has the port. Either stop it or change the port in `.vscode/launch.json`

**Login works but redirects to /login immediately**
→ `SECURE_COOKIES=1` is set in `.env` but you're on HTTP. Set it to `0` for local dev.

**Draft timer fires but page doesn't refresh**
→ Check the browser console for EventSource errors. Make sure you're not blocking `localhost` requests.

**"WARNING: ADMIN_PASSWORD env var not set"**
→ Open `.env` and set `ADMIN_PASSWORD=something`. The fallback password is shown in the terminal.

---

## Deploying to Production

See `docker-compose.yml` and `nginx/nginx.conf`. The short version:

```bash
cp .env.example .env
# Fill in POSTGRES_PASSWORD, JWT_SECRET, ADMIN_PASSWORD with real values
# Drop TLS certs in nginx/certs/fullchain.pem and nginx/certs/privkey.pem
docker compose up -d
```
