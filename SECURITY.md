# Security Policy

## Reporting Security Vulnerabilities

- If you find a security issue, do not open a public issue.
- Please report it responsibly via GitHub Private Vulnerability Reporting or contact the maintainer directly.
- Include a minimal reproduction and the exact version (`josty --version`).

## Threat Model & Boundary

Josty is a local command and Python library, not an authenticated network service. Search
queries are passed to external backends (`ddgs`) over HTTPS. When `--fetch` is used, Josty
downloads and parses web pages via `trafilatura` and `httpx`.

### What is In Scope (Defended)

- **SSRF in `--fetch`**: Josty blocks private, loopback, link-local, multicast, and CGNAT IP ranges across all DNS resolutions and redirects.
- **Resource Exhaustion**: Hard byte limits (`max_download_bytes`) and character limits (`max_content_chars`) protect against memory exhaustion from oversized pages or zip-bomb equivalents.
- **Subprocess Safety**: Queries and arguments are passed directly as process arguments without shell execution (`shell=False`).

### What is Out of Scope (Runtime Responsibility)

The following are deliberately **not** defended by Josty and are the agent runtime's job:

- **Prompt Injection in Fetched Content**: Text extracted from web pages is untrusted data. The agent runtime MUST treat search snippets and fetched markdown as data, never as system instructions.
- **Upstream Backend Trust**: Search results reflect public web search engine rankings and may contain malicious, spam, or misleading URLs.
- **Rate Limiting / IP Reputation**: Upstream providers may throttle or block datacenter IPs. Josty does not implement proxy rotation or CAPTCHA solving.
- **Multi-tenant Isolation**: If hosted behind an API service, authentication and per-tenant isolation belong to the third-party service that wraps Josty, not to the library. The check rejects loopback (`127.0.0.0/8`, `::1/128`),
  link-local (`169.254.0.0/16`, `fe80::/10`, including cloud metadata at `169.254.169.254`),
  private (`10/8`, `172.16/12`, `192.168/16`, `fc00::/7`), and the rest of the IANA-special-purpose
  range (`0.0.0.0/8`, `100.64/10` CGNAT, `192.0.0.0/24`, `192.0.2.0/24`, `198.18/15` benchmark,
  `198.51.100.0/24`, `203.0.113.0/24`, `224/4` multicast, `240/4` reserved, `::/128`, `::ffff:0:0/96`,
  blocked range is caught.
- **Response bounds:** `Content-Type` must be `text/html`, `application/xhtml+xml`, or `text/plain`;
  the decoded body is capped at `max_download_bytes`; the extracted text is capped at
  `max_content_chars`.

### Out of scope — caller's responsibility

The following are deliberately **not** defended by Josty and are the agent runtime's job:

- **DNS rebinding.** DNS is resolved during validation and again at connect time, with no pinning
  or custom transport. An attacker who can observe a public name resolve to a safe address, then flip
  the answer to a loopback / link-local address between hops, can still reach a private target. Any
  agent embedding this in a tool environment must add an egress policy (e.g. `nftables`, eBPF, or a
  sandboxed network namespace) that denies private/loopback/link-local destinations at the network
  layer and/or uses a vetted HTTP client that pins the validated address across the redirect chain.
- **Authenticated egress, quotas, multi-tenant isolation, and monitoring.** These belong to the
  third-party service that wraps Josty, not to the library.
- **Untrusted content as instructions.** Fetched pages are returned as data; the agent runtime is
  responsible for treating them as untrusted and never as tool input or model instructions.
