#!/usr/bin/env python3
"""Open the canonical Lummenna observatory from a local compatibility port.

This small redirect server preserves the historical ``localhost:8850`` entry
point without maintaining a second dashboard implementation or data contract.
Run ``lummenna serve`` first so the canonical frontend is available.
"""

from __future__ import annotations

import http.server
import os
import socketserver

LUMMENNA_BASE = os.environ.get("LUMMENNA_API_URL", "http://localhost:8848").rstrip("/")
PORT = int(os.environ.get("OPENCODE_BOARD_PORT", "8850"))


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        target = f"{LUMMENNA_BASE}/"
        self.send_response(307)
        self.send_header("Location", target)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> None:
    with socketserver.TCPServer(("127.0.0.1", PORT), _Handler) as httpd:
        print(f"OpenCode dashboard redirect running at http://localhost:{PORT}")
        print(f"Canonical Lummenna observatory: {LUMMENNA_BASE}/")
        print("Press Ctrl+C to stop")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down.")


if __name__ == "__main__":
    main()
