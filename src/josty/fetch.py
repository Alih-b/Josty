"""Bounded page download, SSRF validation, and text extraction."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import threading
from datetime import timezone
from urllib.parse import urljoin, urlsplit

try:
    from datetime import UTC
except ImportError:
    UTC = timezone.utc

import httpx

_ALLOWED_FETCH_CONTENT_TYPES = frozenset(
    {"text/html", "application/xhtml+xml", "text/plain"}
)
BROWSER_FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Linux"',
}

_EXTRACT_LOCK: threading.Lock = threading.Lock()


def _content_type_allowed(header: str | None) -> bool:
    """Return True when the Content-Type media type is an allowed fetch type."""
    if not header or not header.strip():
        return False
    media = header.split(";", 1)[0].strip().lower()
    return media in _ALLOWED_FETCH_CONTENT_TYPES


async def download(
    client: httpx.AsyncClient,
    url: str,
    *,
    timeout: float,
    max_download_bytes: int,
    validate,
) -> tuple[str, str]:
    current = url
    for _ in range(6):
        await validate(current)
        async with client.stream("GET", current) as response:
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    raise ValueError("redirect response has no location")
                current = urljoin(current, location)
                continue
            response.raise_for_status()
            content_type = response.headers.get("content-type")
            if not _content_type_allowed(content_type):
                raise ValueError(
                    f"unsupported content type: {content_type or 'missing'}"
                )
            content_length = response.headers.get("content-length")
            if (
                content_length
                and content_length.isdigit()
                and int(content_length) > max_download_bytes
            ):
                raise ValueError("response exceeds download limit")
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > max_download_bytes:
                    raise ValueError("response exceeds download limit")
                chunks.append(chunk)
            encoding = response.encoding or "utf-8"
            return b"".join(chunks).decode(encoding, errors="replace"), str(response.url)
    raise ValueError("too many redirects")


async def validate_public_url(url: str, *, timeout: float) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("only public HTTP(S) URLs can be fetched")
    if parsed.username or parsed.password:
        raise ValueError("URLs containing credentials are blocked")
    default_port = 443 if parsed.scheme == "https" else 80
    try:
        addresses = await asyncio.wait_for(
            asyncio.to_thread(
                socket.getaddrinfo,
                parsed.hostname,
                parsed.port or default_port,
                type=socket.SOCK_STREAM,
            ),
            timeout=timeout,
        )
    except TimeoutError as exc:
        raise ValueError("hostname resolution timed out") from exc
    except socket.gaierror as exc:
        raise ValueError("hostname could not be resolved") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global or ip.is_multicast:
            raise ValueError("private or reserved network destinations are blocked")


def is_ad_redirect(url: str) -> bool:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    path = parsed.path.lower()

    def is_domain(domain: str) -> bool:
        return host == domain or host.endswith(f".{domain}")

    return (
        (is_domain("google.com") and path.startswith("/aclick"))
        or (is_domain("bing.com") and path.startswith("/ck/"))
        or is_domain("googleadservices.com")
        or is_domain("doubleclick.net")
    )


def extract(html: str, url: str) -> tuple[str, str]:
    with _EXTRACT_LOCK:
        try:
            import trafilatura

            extracted = trafilatura.extract(
                html, url=url, include_links=True, output_format="markdown"
            )
            if extracted and extracted.strip():
                return extracted.strip(), "trafilatura"
        except Exception:
            pass
        without_noise = re.sub(
            r"<(script|style|noscript)\b[^>]*>.*?</\1>",
            " ",
            html,
            flags=re.IGNORECASE | re.DOTALL,
        )
        fallback = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", without_noise)).strip()
        return fallback, "html-text-fallback"
