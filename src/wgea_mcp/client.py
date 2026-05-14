"""Async fetcher for data.gov.au CKAN metadata and WGEA ZIP downloads.

Two endpoints:
- `fetch_package(name)`  — CKAN `package_show` for a dataset slug. JSON.
                            Cached as "catalog" (6h).
- `fetch_resource(url)`  — pulls a static ZIP/CSV/XLSX by URL. Cached as "data"
                            (30 days — WGEA releases annually).

data.gov.au is CKAN under the Drupal 11 wrapper. The CKAN API path is
`/data/api/3/action/...`. Public, no auth, no documented rate limit. We send
a courteous User-Agent and dedupe concurrent in-flight requests for the
same URL so a burst of `latest()` calls fans in to one HTTP request.

The host whitelist accepts data.gov.au + subdomains. Defense-in-depth against
any future CKAN response that hands back an off-host URL.
"""
from __future__ import annotations

import asyncio
import json
from collections import OrderedDict
from typing import Any

import httpx

from .cache import TTL, Cache, CacheKind

DEFAULT_BASE_URL = "https://data.gov.au"
DEFAULT_TIMEOUT = httpx.Timeout(180.0, connect=15.0)  # 2025 ZIP is ~71MB

_ALLOWED_HOST_SUFFIXES = ("data.gov.au",)

# How many recent fetch results to keep in-memory to defeat the SQLite
# read-after-write race. Each entry is the raw bytes of a fetched resource;
# memory cost is bounded because we cap at this many entries (LRU).
_RECENT_RESULTS_MAX_ENTRIES = 16


class WGEAAPIError(Exception):
    """Raised when data.gov.au returns non-2xx or the request fails."""


def _is_allowed_host(url: str) -> bool:
    """True only when the URL's host is data.gov.au or a subdomain AND the
    scheme is http(s). Defense-in-depth — a CKAN response or seed manifest
    that ever hands back ftp://data.gov.au/... must not slip through.
    """
    try:
        from urllib.parse import urlparse

        parsed = urlparse(url)
    except (ValueError, AttributeError):
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    return any(host == suf or host.endswith("." + suf) for suf in _ALLOWED_HOST_SUFFIXES)


class WGEAClient:
    def __init__(
        self,
        cache: Cache | None = None,
        base_url: str = DEFAULT_BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.cache = cache or Cache()
        self._http = httpx.AsyncClient(
            timeout=DEFAULT_TIMEOUT,
            transport=transport,
            headers={
                "User-Agent": "wgea-mcp/0.1 (+https://github.com/Bigred97/wgea-mcp)",
                "Accept": "*/*",
            },
            follow_redirects=True,
        )
        self._in_flight: dict[str, asyncio.Future[bytes]] = {}
        self._in_flight_lock = asyncio.Lock()
        # SQLite (even in WAL mode) gives readers a snapshot from when their
        # connection opened. A reader that opens its connection *before* the
        # writer commits will see a stale snapshot — i.e. MISS — even if its
        # SELECT runs after the commit. Without mitigation, this means a burst
        # of 50 concurrent callers can produce 2-3 HTTP requests for the same
        # URL because some late-arriving callers find no in-flight future
        # (the first caller already popped it) AND no cache entry (stale
        # snapshot). This in-memory LRU is consulted between the SQLite
        # cache.get and the in-flight registration to defeat the race.
        self._recent_results: OrderedDict[str, bytes] = OrderedDict()
        self._recent_results_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> WGEAClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def fetch_resource(self, url: str, *, kind: CacheKind = "data") -> bytes:
        """Fetch a static file (ZIP/CSV/XLSX) by URL. Cached. In-flight deduped."""
        if not url.startswith(("http://", "https://")):
            raise WGEAAPIError(f"Refusing to fetch non-http(s) URL: {url!r}")
        if not _is_allowed_host(url):
            raise WGEAAPIError(
                f"Refusing to fetch off-host URL {url!r}. "
                "wgea-mcp only fetches from data.gov.au."
            )
        return await self._fetch_cached(url, kind=kind)

    async def fetch_package(self, package_id: str) -> dict[str, Any]:
        """Fetch CKAN package_show for a dataset slug. Returns the result dict.

        `package_id` is the data.gov.au dataset slug (e.g. 'wgea-dataset').
        Raises WGEAAPIError if the dataset doesn't exist or CKAN returns
        success=false.
        """
        if "/" in package_id or "?" in package_id or "&" in package_id:
            raise WGEAAPIError(f"Bad package id: {package_id!r}")
        url = f"{self.base_url}/data/api/3/action/package_show?id={package_id}"
        body = await self._fetch_cached(url, kind="catalog")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise WGEAAPIError(f"CKAN returned non-JSON for {package_id!r}: {e}") from e
        if not payload.get("success"):
            err = payload.get("error", {})
            raise WGEAAPIError(f"CKAN error for {package_id!r}: {err}")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise WGEAAPIError(f"CKAN result missing for {package_id!r}")
        return result

    async def _fetch_cached(self, url: str, *, kind: CacheKind) -> bytes:
        cached = await self.cache.get(url, ttl=TTL[kind])
        if cached is not None:
            return cached

        # Defeat the SQLite read-after-write race: late-arriving callers
        # whose `cache.get` returned MISS despite a recent write can still
        # find the bytes in the in-memory recent-results LRU.
        async with self._recent_results_lock:
            recent = self._recent_results.get(url)
            if recent is not None:
                self._recent_results.move_to_end(url)
                return recent

        async with self._in_flight_lock:
            existing = self._in_flight.get(url)
            if existing is None:
                future: asyncio.Future[bytes] = (
                    asyncio.get_running_loop().create_future()
                )
                self._in_flight[url] = future

        if existing is not None:
            return await existing

        try:
            try:
                resp = await self._http.get(url)
                resp.raise_for_status()
            except httpx.HTTPStatusError as e:
                raise WGEAAPIError(
                    f"data.gov.au returned {e.response.status_code} for {url}"
                ) from e
            except httpx.RequestError as e:
                raise WGEAAPIError(f"data.gov.au request failed: {e}") from e
            await self.cache.set(
                url,
                resp.content,
                kind=kind,
                etag=resp.headers.get("etag"),
                last_modified=resp.headers.get("last-modified"),
            )
            # Populate the in-memory LRU BEFORE setting the future result so
            # waiters that fall through into a future cache.get miss can use
            # the in-memory hit.
            async with self._recent_results_lock:
                self._recent_results[url] = resp.content
                self._recent_results.move_to_end(url)
                while len(self._recent_results) > _RECENT_RESULTS_MAX_ENTRIES:
                    self._recent_results.popitem(last=False)
            future.set_result(resp.content)
            return resp.content
        except BaseException as e:
            if not future.done():
                future.set_exception(e)
            raise
        finally:
            async with self._in_flight_lock:
                self._in_flight.pop(url, None)
