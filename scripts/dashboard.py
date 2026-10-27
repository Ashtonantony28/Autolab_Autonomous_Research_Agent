"""Streamlit entry for the run-audit dashboard.

    make dashboard
    uv run streamlit run scripts/dashboard.py -- --db sqlite+aiosqlite:///./autolab.db

Without `--db` it reads `DATABASE_URL` (see `.env.example`).
"""

from __future__ import annotations

import argparse

from autolab.config import get_settings
from autolab.monitoring.dashboard import main

parser = argparse.ArgumentParser()
parser.add_argument("--db", default=None, help="SQLAlchemy URL of the state store")
main(parser.parse_known_args()[0].db or get_settings().database_url)
