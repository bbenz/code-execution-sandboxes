"""sentinel: a deny-all, logging HTTP(S) proxy that stands in for the Sandboxes egress proxy locally.

The local Docker backend puts candidate containers on an internal network whose only exit is this
proxy. Every request is refused with 403 and logged as one JSON line, so local runs produce the same
kind of egress audit trail that Azure Container Apps Sandboxes records for you.
"""

from __future__ import annotations

import asyncio
import json
import sys
from urllib.parse import urlsplit

from referee.util import utc_now

BODY = b"Blocked by the referee sentinel (default-deny egress)\n"
RESPONSE = (
    b"HTTP/1.1 403 Forbidden\r\nContent-Type: text/plain\r\nConnection: close\r\n"
    + f"Content-Length: {len(BODY)}\r\n\r\n".encode()
    + BODY
)


def parse_request_line(line: str) -> dict:
    parts = line.split(" ")
    method = parts[0] if parts else ""
    target = parts[1] if len(parts) > 1 else ""
    if method == "CONNECT":
        host, _, port = target.rpartition(":")
        return {"method": method, "scheme": "https", "host": host or target, "port": port, "path": ""}
    url = urlsplit(target)
    return {"method": method, "scheme": url.scheme or "http", "host": url.hostname or "",
            "port": str(url.port or ""), "path": url.path or "/"}


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    client = (writer.get_extra_info("peername") or ("?",))[0]
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
        request = parse_request_line(head.split(b"\r\n", 1)[0].decode("latin-1"))
        print(json.dumps({"timestamp": utc_now(), "action": "Deny", "client": client, **request}), flush=True)
        writer.write(RESPONSE)
        await writer.drain()
    except (TimeoutError, asyncio.IncompleteReadError, ConnectionError, asyncio.LimitOverrunError):
        pass
    finally:
        writer.close()


async def serve(port: int) -> None:
    server = await asyncio.start_server(_handle, host="0.0.0.0", port=port)
    print(json.dumps({"timestamp": utc_now(), "event": "listening", "port": port}), flush=True)
    async with server:
        await server.serve_forever()


def run(port: int = 3128) -> int:
    try:
        asyncio.run(serve(port))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(run())
