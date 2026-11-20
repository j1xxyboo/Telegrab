"""Dev entrypoint: python run.py"""
from __future__ import annotations

import os

import uvicorn

from app import config

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=config.HOST,
        port=config.PORT,
        reload=os.environ.get("TELE_RELOAD", "0") == "1",
        log_level="info",
    )
