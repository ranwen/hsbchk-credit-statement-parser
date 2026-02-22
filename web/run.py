from __future__ import annotations

import json
from pathlib import Path

import uvicorn

from app import app as fastapi_app

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"


def main() -> None:
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    host = cfg.get("server", {}).get("host", "127.0.0.1")
    port = int(cfg.get("server", {}).get("port", 8000))
    uvicorn.run(fastapi_app, host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
