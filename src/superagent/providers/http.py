"""HTTP plumbing shared by httpx-based adapters: status → ProviderStatus mapping, downloads."""

from __future__ import annotations

import email.utils
import time
from pathlib import Path
from typing import Any

import httpx

from ..errors import ProviderError
from ..security.secrets import mask
from ..types import ProviderStatus

DEFAULT_TIMEOUT = httpx.Timeout(120.0, connect=10.0)
LIMITS = httpx.Limits(max_connections=20, max_keepalive_connections=10)


def new_client(*, headers: dict[str, str] | None = None, timeout: httpx.Timeout | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(headers=headers, timeout=timeout or DEFAULT_TIMEOUT, limits=LIMITS, follow_redirects=True)


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        parsed = email.utils.parsedate_to_datetime(raw) if raw else None
        return max(0.0, parsed.timestamp() - time.time()) if parsed else None


def request_error(provider: str, message: str) -> ProviderError:
    """The request itself was rejected (4xx). The provider is healthy, so it is not penalised."""
    return ProviderError(provider, ProviderStatus.UNAVAILABLE, message, penalize=False)


def raise_for_status(provider: str, response: httpx.Response) -> None:
    code = response.status_code
    if code < 400:
        return
    excerpt = mask(response.text[:400].replace("\n", " ")) if response.text else ""
    detail = f"HTTP {code}" + (f": {excerpt}" if excerpt else "")
    if code in (401, 403):
        raise ProviderError(
            provider, ProviderStatus.AUTH_ERROR, detail, hint="check the API key and its permissions"
        )
    if code == 402:
        raise ProviderError(provider, ProviderStatus.UNAVAILABLE, detail, hint="the account has no credit or billing")
    if code == 429:
        raise ProviderError(provider, ProviderStatus.RATE_LIMITED, detail, retry_after=_retry_after(response))
    if code in (408, 409, 425) or code >= 500:
        raise ProviderError(
            provider, ProviderStatus.TEMPORARY_ERROR, detail, retry_after=_retry_after(response)
        )
    if code == 404:
        raise ProviderError(provider, ProviderStatus.UNAVAILABLE, detail, hint="check the model name and base URL")
    raise request_error(provider, detail)


async def send(provider: str, client: httpx.AsyncClient, method: str, url: str, **kwargs: Any) -> httpx.Response:
    try:
        response = await client.request(method, url, **kwargs)
    except httpx.TimeoutException as exc:
        raise ProviderError(provider, ProviderStatus.TEMPORARY_ERROR, f"request timed out ({type(exc).__name__})") from exc
    except httpx.TransportError as exc:
        raise ProviderError(provider, ProviderStatus.NETWORK_ERROR, f"cannot reach endpoint: {exc}") from exc
    raise_for_status(provider, response)
    return response


def json_body(provider: str, response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        raise ProviderError(provider, ProviderStatus.TEMPORARY_ERROR, "endpoint returned a non-JSON body") from exc


async def download(
    provider: str, client: httpx.AsyncClient, url: str, dest: Path, *, max_bytes: int = 600_000_000
) -> Path:
    """Stream a result file to disk with a size cap. Only http(s) URLs are accepted."""
    if not url.startswith(("https://", "http://")):
        raise ProviderError(provider, ProviderStatus.TEMPORARY_ERROR, "result URL has an unexpected scheme")
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_suffix(dest.suffix + ".part")
    written = 0
    try:
        async with client.stream("GET", url) as response:
            raise_for_status(provider, response)
            with open(partial, "wb") as handle:
                async for chunk in response.aiter_bytes(1 << 16):
                    written += len(chunk)
                    if written > max_bytes:
                        raise ProviderError(provider, ProviderStatus.TEMPORARY_ERROR, "result exceeds the size cap")
                    handle.write(chunk)
    except httpx.TransportError as exc:
        partial.unlink(missing_ok=True)
        raise ProviderError(provider, ProviderStatus.NETWORK_ERROR, f"download failed: {exc}") from exc
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    if written == 0:
        partial.unlink(missing_ok=True)
        raise ProviderError(provider, ProviderStatus.TEMPORARY_ERROR, "result file was empty")
    partial.replace(dest)
    return dest
