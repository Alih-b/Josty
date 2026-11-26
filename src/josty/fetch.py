"""Bounded page download, SSRF validation, and text extraction."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import threading
from urllib.parse import urljoin, urlsplit

import httpx

from .status import USER_AGENT

_ALLOWED_FETCH_CONTENT_TYPES = frozenset(
    {"text/html", "application/xhtml+xml", "text/plain"}
)
FETCH_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
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


#: The RFC 2544 benchmarking range. Proxy and VPN clients in fake-IP mode answer
#: every DNS lookup with an address from it and route the connection themselves
#: (#129). It is not global, so it is blocked unless the caller opts in.
FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")


async def validate_public_url(
    url: str, *, timeout: float, allow_fake_ip: bool = False
) -> None:
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
        in_fake_range = ip.version == 4 and ip in FAKE_IP_NETWORK
        if in_fake_range and allow_fake_ip:
            continue
        if not ip.is_global or ip.is_multicast:
            message = "private or reserved network destinations are blocked"
            if in_fake_range:
                message += (
                    f" ({ip} is in {FAKE_IP_NETWORK}, the fake-IP range of proxy and"
                    " VPN clients; set JOSTY_ALLOW_FAKE_IP=1 or pass --allow-fake-ip"
                    " to allow it)"
                )
            raise ValueError(message)


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


#: A page with at least this many table rows gets a second extraction pass (#128).
_MIN_TABLE_ROWS = 5


def _table_lines(markdown: str | None) -> int:
    return sum(1 for line in (markdown or "").splitlines() if line.startswith("|"))


def _extract_with_unlinked_tables(html: str, url: str) -> str | None:
    """trafilatura's markdown of ``html`` with the links inside tables unwrapped.

    trafilatura drops a table whose text is mostly links (its link-density test),
    and a table of winners or releases usually is exactly that. Unwrapping the
    anchors keeps the cell text and gives up only the link targets in the table.
    """
    import lxml.html
    import trafilatura

    document = lxml.html.fromstring(html)
    for anchor in document.xpath("//table//a"):
        anchor.drop_tag()
    return trafilatura.extract(
        lxml.html.tostring(document, encoding="unicode"),
        url=url,
        include_links=True,
        output_format="markdown",
    )


def extract(html: str, url: str) -> tuple[str, str]:
    with _EXTRACT_LOCK:
        try:
            import trafilatura

            extracted = trafilatura.extract(
                html, url=url, include_links=True, output_format="markdown"
            )
            if len(re.findall(r"<tr[\s>]", html, flags=re.IGNORECASE)) >= _MIN_TABLE_ROWS:
                try:
                    unlinked = _extract_with_unlinked_tables(html, url)
                except Exception:
                    unlinked = None
                if unlinked and _table_lines(unlinked) > _table_lines(extracted):
                    extracted = unlinked
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


#: Characters of context on each side of a quote: a W3C TextQuoteSelector's
#: prefix and suffix.
QUOTE_CONTEXT_CHARS = 32


def text_quote(text: str, phrase: str) -> dict[str, str] | None:
    """A W3C TextQuoteSelector for an exact phrase of ``text``, or None (#107).

    Exact match only: no fuzzy or near-match, so a quote is always a span the
    stored text really contains. The first occurrence wins.
    """
    if not phrase or not text:
        return None
    start = text.find(phrase)
    if start < 0:
        return None
    end = start + len(phrase)
    return {
        "type": "TextQuoteSelector",
        "exact": phrase,
        "prefix": text[max(0, start - QUOTE_CONTEXT_CHARS) : start],
        "suffix": text[end : end + QUOTE_CONTEXT_CHARS],
    }
