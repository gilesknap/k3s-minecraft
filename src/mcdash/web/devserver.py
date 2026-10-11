"""Run the UI against a seeded in-memory fake: ``python -m mcdash.web.devserver``.

Serves on http://localhost:8080 (override with ``--port``). States advance on
their own (seeding → starting → running) and the log tail follows forever.
Send ``X-Auth-Request-Email`` to impersonate a user; otherwise "unknown".
"""

from __future__ import annotations

import argparse

import uvicorn

from mcdash.web.app import create_app
from mcdash.web.fake import seeded


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    app = create_app(seeded(live_logs=True, simulate=True))
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
