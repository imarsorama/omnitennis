"""Start the app: ``python -m app``.

Reads the HTTP port from the ``PORT`` environment variable (default 8000) and
binds to 127.0.0.1 only, as expected when running behind a local reverse proxy.
"""

from __future__ import annotations

import os

import uvicorn
from dotenv import load_dotenv


def main() -> None:
    load_dotenv()
    port = int(os.environ.get("PORT", 8000))
    reload = os.environ.get("RELOAD", "").lower() in ("1", "true", "yes")
    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=port,
        reload=reload,
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
    )


if __name__ == "__main__":
    main()
