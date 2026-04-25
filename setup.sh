#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# Gridiron Fantasy — First-time setup
# Run this once from the project root:   bash setup.sh
# ──────────────────────────────────────────────────────────────────────────────

set -e  # exit immediately on any error

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║         Gridiron Fantasy — Dev Setup                ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""

# ── 1. Check Python version ───────────────────────────────────────────────────
PYTHON=$(command -v python3 || command -v python)
if [ -z "$PYTHON" ]; then
  echo "❌  Python not found. Install Python 3.11+ from https://python.org"
  exit 1
fi

PY_VERSION=$($PYTHON -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MAJOR=$($PYTHON -c "import sys; print(sys.version_info.major)")
PY_MINOR=$($PYTHON -c "import sys; print(sys.version_info.minor)")

echo "✅  Found Python $PY_VERSION"

if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 11 ]; }; then
  echo "⚠️   Python 3.11+ recommended (you have $PY_VERSION). Some type hints may not work."
fi

# ── 2. Create virtual environment ─────────────────────────────────────────────
if [ ! -d ".venv" ]; then
  echo ""
  echo "▶  Creating virtual environment at .venv/ ..."
  $PYTHON -m venv .venv
  echo "✅  Virtual environment created"
else
  echo "✅  Virtual environment already exists (.venv/)"
fi

# ── 3. Activate and install deps ─────────────────────────────────────────────
echo ""
echo "▶  Installing dependencies from requirements.txt ..."

if [[ "$OSTYPE" == "msys" || "$OSTYPE" == "win32" || -n "$WINDIR" ]]; then
  # Windows (Git Bash / PowerShell)
  VENV_PIP=".venv/Scripts/pip"
  VENV_PYTHON=".venv/Scripts/python"
else
  # macOS / Linux
  VENV_PIP=".venv/bin/pip"
  VENV_PYTHON=".venv/bin/python"
fi

$VENV_PIP install --upgrade pip --quiet
$VENV_PIP install -r requirements.txt --quiet

echo "✅  All packages installed"

# ── 4. Copy .env if missing ───────────────────────────────────────────────────
echo ""
if [ ! -f ".env" ]; then
  cp .env.example .env
  echo "✅  Created .env from .env.example"
  echo "   ⚠️  Open .env and set ADMIN_PASSWORD before running the app."
else
  echo "✅  .env already exists"
fi

# ── 5. Create data directory ──────────────────────────────────────────────────
mkdir -p data
echo "✅  data/ directory ready (SQLite DB will be created here on first run)"

# ── 6. Done ───────────────────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║  Setup complete! Next steps:                        ║"
echo "║                                                      ║"
echo "║  1. Open VS Code:   code .                          ║"
echo "║  2. When prompted, select the Python interpreter:   ║"
echo "║       .venv/bin/python  (or .venv\\Scripts\\python)  ║"
echo "║  3. Press F5 to launch the app                      ║"
echo "║  4. Open http://localhost:8000  in your browser     ║"
echo "║                                                      ║"
echo "║  Default login:  admin / admin123                   ║"
echo "║  (set in .env → ADMIN_PASSWORD)                     ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""
