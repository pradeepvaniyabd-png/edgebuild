"""Web search and fetch with SSRF protection.

Fetching is limited to http(s) on public addresses unless SAX_ALLOW_PRIVATE_NETWORK
is set. Redirects are followed manually so every hop is re-validated.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx
from pydantic import BaseModel, Field

from ..errors import DependencyMissing, ProviderError, SecurityError, ToolError
from ..types import PermissionLevel, ProviderStatus, RiskLevel
from .registry import ToolContext, ToolRegistry, ToolSpec

_UA = "SuperAgentX/0.1 (+local research assistant)"
_SKIP_TAGS = {"script", "style", "noscript", "svg", "template", "head"}
_BLOCK_TAGS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "table"}


async def validate_url(url: str, *, allow_private: bool) -> str:
    """Reject non-http(s) URLs and, by default, anything that resolves to a non-public address."""
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        raise SecurityError(f"only http(s) URLs can be fetched, not '{parts.scheme or 'none'}'")
    if not parts.hostname:
        raise SecurityError("URL has no host")
    if parts.username or parts.password:
        raise SecurityError("URLs with embedded credentials are not allowed")
    if allow_private:
        return url
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(parts.hostname, parts.port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ToolError(f"cannot resolve host '{parts.hostname}'") from exc
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global or address.is_multicast:
            raise SecurityError(
                f"'{parts.hostname}' resolves to a non-public address ({address})",
                hint="set SAX_ALLOW_PRIVATE_NETWORK=true to reach private hosts on purpose",
            )
    return url


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self.links: list[dict[str, str]] = []
        self._skip = 0
        self._in_title = False
        self._link: dict[str, str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip += 1
            self._in_title = False
        if tag == "title":
            self._in_title = True
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")
        if tag == "a":
            href = dict(attrs).get("href")
            if href and not href.startswith(("#", "javascript:", "mailto:")):
                self._link = {"href": href, "text": ""}

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip:
            self._skip -= 1
        if tag == "title":
            self._in_title = False
        if tag == "a" and self._link is not None:
            if self._link["text"].strip():
                self.links.append({"href": self._link["href"], "text": " ".join(self._link["text"].split())[:120]})
            self._link = None

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
            return
        if self._skip:
            return
        self.parts.append(data)
        if self._link is not None:
            self._link["text"] += data


def html_to_text(html: str) -> tuple[str, str, list[dict[str, str]]]:
    parser = _Text()
    parser.feed(html)
    lines = [" ".join(line.split()) for line in "".join(parser.parts).splitlines()]
    text = "\n".join(line for line in lines if line)
    return text, " ".join(parser.title.split()), parser.links


class FetchInput(BaseModel):
    url: str
    max_bytes: int = Field(2_000_000, ge=1000, le=20_000_000)
    max_chars: int = Field(60_000, ge=500, le=500_000)


async def web_fetch(args: FetchInput, ctx: ToolContext) -> dict[str, Any]:
    allow_private = ctx.settings.allow_private_network
    url = args.url
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0), headers={"User-Agent": _UA}) as client:
        for _hop in range(6):
            await validate_url(url, allow_private=allow_private)
            try:
                async with client.stream("GET", url) as response:
                    if response.status_code in (301, 302, 303, 307, 308) and "location" in response.headers:
                        url = urljoin(url, response.headers["location"])
                        continue
                    body = bytearray()
                    async for chunk in response.aiter_bytes(1 << 15):
                        body += chunk
                        if len(body) > args.max_bytes:
                            break
                    status = response.status_code
                    content_type = response.headers.get("content-type", "")
                    encoding = response.encoding or "utf-8"
            except httpx.HTTPError as exc:
                raise ToolError(f"could not fetch {urlsplit(url).hostname}: {type(exc).__name__}") from exc
            break
        else:
            raise ToolError("too many redirects")
    raw = bytes(body[: args.max_bytes]).decode(encoding, errors="replace")
    if "html" in content_type or raw.lstrip()[:15].lower().startswith(("<!doctype", "<html")):
        text, title, links = html_to_text(raw)
        links = [{"href": urljoin(url, link["href"]), "text": link["text"]} for link in links[:60]]
    else:
        text, title, links = raw, "", []
    return {
        "url": url, "status": status, "content_type": content_type, "title": title,
        "text": text[: args.max_chars], "truncated": len(text) > args.max_chars or len(body) > args.max_bytes,
        "links": links,
    }  # fmt: skip


class SearchInput(BaseModel):
    query: str = Field(min_length=1, max_length=400)
    max_results: int = Field(8, ge=1, le=25)


class _DuckResults(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._field: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        attributes = dict(attrs)
        classes = (attributes.get("class") or "").split()
        if "result__a" in classes:
            self.results.append({"title": "", "url": _unwrap(attributes.get("href") or ""), "snippet": ""})
            self._field = "title"
        elif "result__snippet" in classes and self.results:
            self._field = "snippet"

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self._field = None

    def handle_data(self, data: str) -> None:
        if self._field and self.results:
            self.results[-1][self._field] += data


def _unwrap(href: str) -> str:
    """DuckDuckGo wraps result links in a redirect; recover the real target."""
    if href.startswith("//"):
        href = "https:" + href
    target = parse_qs(urlsplit(href).query).get("uddg")
    return target[0] if target else href


async def web_search(args: SearchInput, ctx: ToolContext) -> dict[str, Any]:
    backend = ctx.settings.web_search
    if backend == "none":
        raise DependencyMissing(
            "web search is disabled", hint="set SAX_WEB_SEARCH=duckduckgo, or SAX_WEB_SEARCH=searxng with SEARXNG_URL"
        )
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(20.0, connect=10.0), headers={"User-Agent": _UA}, follow_redirects=True
    ) as client:
        try:
            if backend == "searxng":
                if not ctx.settings.searxng_url:
                    raise DependencyMissing("SEARXNG_URL is not set")
                response = await client.get(
                    f"{ctx.settings.searxng_url.rstrip('/')}/search", params={"q": args.query, "format": "json"}
                )
                response.raise_for_status()
                rows = response.json().get("results", [])
                results = [{"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("content", "")} for r in rows]
            else:
                response = await client.post("https://html.duckduckgo.com/html/", data={"q": args.query})
                response.raise_for_status()
                parser = _DuckResults()
                parser.feed(response.text)
                results = parser.results
        except httpx.HTTPStatusError as exc:
            status = ProviderStatus.RATE_LIMITED if exc.response.status_code in (202, 429) else ProviderStatus.TEMPORARY_ERROR
            raise ProviderError(f"search:{backend}", status, f"HTTP {exc.response.status_code}") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"search:{backend}", ProviderStatus.NETWORK_ERROR, type(exc).__name__) from exc
    cleaned = [
        {"title": " ".join(r["title"].split()), "url": r["url"], "snippet": " ".join(r["snippet"].split())}
        for r in results
        if r.get("url", "").startswith("http")
    ]
    return {"query": args.query, "backend": backend, "results": cleaned[: args.max_results]}


def register(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec("web.search", "Search the web and return titles, URLs and snippets.", SearchInput, web_search,
                 PermissionLevel.READ_ONLY, RiskLevel.LOW, 45)  # fmt: skip
    )
    registry.register(
        ToolSpec("web.fetch", "Download a public web page and return its readable text and links.", FetchInput, web_fetch,
                 PermissionLevel.READ_ONLY, RiskLevel.MEDIUM, 90)  # fmt: skip
    )
    registry.register(
        ToolSpec("browser.open", "Open a URL and return the page text (static fetch; no JavaScript execution).",
                 FetchInput, web_fetch, PermissionLevel.READ_ONLY, RiskLevel.MEDIUM, 90)  # fmt: skip
    )
