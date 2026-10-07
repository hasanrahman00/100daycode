"""DNS lookups through public DNS servers (Cloudflare, Google, Quad9) instead of the system resolver.

Home and office routers often can't keep up with thousands of lookups a second, so most sites
fail with DNS errors or timeouts. This resolver sends the queries straight to public servers over
UDP from the event loop (no thread per lookup) and caches answers.
"""
import asyncio
import ipaddress
import socket

import dns.asyncresolver
import dns.exception
import dns.resolver
from aiohttp.abc import AbstractResolver

DEFAULT_SERVERS = ["1.1.1.1", "8.8.8.8", "9.9.9.9", "1.0.0.1", "8.8.4.4"]


class PublicDNSResolver(AbstractResolver):
    def __init__(self, servers=None, timeout: float = 3.0, max_in_flight: int = 500):
        self._resolver = dns.asyncresolver.Resolver(configure=False)
        self._resolver.nameservers = list(servers or DEFAULT_SERVERS)
        self._resolver.timeout = timeout          # per server try
        self._resolver.lifetime = timeout * 2     # whole lookup
        self._resolver.rotate = True              # spread load over the servers
        self._cache: dict[str, list[str]] = {}
        self._limit = asyncio.Semaphore(max_in_flight)

    async def _lookup(self, host: str) -> list[str]:
        if host in self._cache:
            return self._cache[host]
        async with self._limit:
            try:
                answer = await self._resolver.resolve(host, "A", search=False)
            except dns.resolver.NXDOMAIN:
                raise OSError(f"DNS: {host} does not exist")
            except dns.resolver.NoAnswer:
                raise OSError(f"DNS: {host} has no IPv4 address")
            except dns.exception.Timeout:
                raise OSError(f"DNS: lookup for {host} timed out")
            except dns.exception.DNSException as e:
                raise OSError(f"DNS: {type(e).__name__} for {host}")
        ips = [r.address for r in answer]
        if len(self._cache) > 200_000:
            self._cache.clear()
        self._cache[host] = ips
        return ips

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET):
        try:
            ipaddress.ip_address(host)  # already an IP
            ips = [host]
        except ValueError:
            ips = await self._lookup(host)
        return [{"hostname": host, "host": ip, "port": port, "family": socket.AF_INET,
                 "proto": 0, "flags": socket.AI_NUMERICHOST} for ip in ips]

    async def close(self) -> None:
        pass
