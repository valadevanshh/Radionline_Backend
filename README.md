# Radionline Backend

FastAPI API for RadioNet PACS.

## Setup

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/macOS:
# source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit DATABASE_URL, JWT_SECRET, etc.
python run.py
```

API: http://127.0.0.1:8000

Password reset helper: `python reset_password.py --help`

Repo: https://github.com/valadevanshh/Radionline_Backend
