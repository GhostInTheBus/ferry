"""Reach Tailscale machines from inside Docker.

Synology's Tailscale package runs in userspace mode, so the NAS has no
tailscale0 interface and containers can't open TCP connections to 100.x
addresses. `tailscale nc` through the tailscaled socket can, so Ferry uses
it: as the SSH proxy command for SFTP, and behind a small local TCP
forwarder for SMB.
"""
from __future__ import annotations

import asyncio
import ipaddress
import os
from typing import Optional

BIN = os.environ.get("FERRY_TAILSCALE_BIN", "/usr/local/bin/tailscale")
SOCK = os.environ.get("FERRY_TAILSCALE_SOCKET", "/var/run/tailscale/tailscaled.sock")
_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_TS6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")


def available() -> bool:
    return os.path.exists(SOCK) and os.access(BIN, os.X_OK)


def is_tailnet_host(host: str) -> bool:
    host = (host or "").strip().lower()
    try:
        ip = ipaddress.ip_address(host)
        return ip in _CGNAT or ip in _TS6
    except ValueError:
        return host.endswith(".ts.net") or ("." not in host and bool(host))


def wanted(cfg: dict) -> bool:
    via = cfg.get("via") or "auto"
    if via == "direct":
        return False
    if via == "tailscale":
        return True
    host = (cfg.get("host") or "").strip()
    try:
        ipaddress.ip_address(host)
        is_ip = True
    except ValueError:
        is_ip = False
    # auto: 100.x addresses and *.ts.net names go through Tailscale when it's mounted
    return available() and (host.endswith(".ts.net") or (is_ip and is_tailnet_host(host)))


def nc_command(host: str, port: int) -> list[str]:
    return [BIN, f"--socket={SOCK}", "nc", host, str(port)]


async def _pump(reader: asyncio.StreamReader, writer) -> None:
    try:
        while True:
            data = await reader.read(1 << 16)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        try:
            if hasattr(writer, "write_eof") and writer.can_write_eof():
                writer.write_eof()
            else:
                writer.close()
        except Exception:
            pass


class Forwarder:
    """127.0.0.1:<random> -> host:port over `tailscale nc`."""

    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.server: Optional[asyncio.base_events.Server] = None
        self.local_port = 0

    async def start(self) -> int:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.local_port = self.server.sockets[0].getsockname()[1]
        return self.local_port

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        proc = await asyncio.create_subprocess_exec(
            *nc_command(self.host, self.port), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            await asyncio.gather(_pump(reader, proc.stdin), _pump(proc.stdout, writer))
        finally:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            writer.close()

    async def close(self) -> None:
        if self.server:
            self.server.close()
            try:
                await asyncio.wait_for(self.server.wait_closed(), 2)
            except Exception:
                pass
