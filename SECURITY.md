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

- **SSRF in `--fetch`**: Josty blocks private, loopback, link-local, multicast, CGNAT, and
  other IANA special-purpose ranges on every DNS resolution and redirect hop: loopback
  (`127.0.0.0/8`, `::1/128`), link-local (`169.254.0.0/16`, `fe80::/10`, including cloud
  metadata at `169.254.169.254`), private (`10/8`, `172.16/12`, `192.168/16`, `fc00::/7`),
  unspecified (`0.0.0.0/8`, `::/128`), CGNAT (`100.64/10`), documentation/benchmark nets
  (`192.0.0.0/24`, `192.0.2.0/24`, `198.18/15`, `198.51.100.0/24`, `203.0.113.0/24`),
  multicast (`224/4`, `ff00::/8`), reserved (`240/4`), and IPv4-mapped IPv6
  (`::ffff:0:0/96`).
- **Response bounds**: `Content-Type` media type (the token before `;`) must be exactly
  `text/html`, `application/xhtml+xml`, or `text/plain`; missing Content-Type is rejected.
  The decoded body is capped at `max_download_bytes`; extracted text is capped at
  `max_content_chars`.
- **Resource exhaustion**: those byte and character limits protect against oversized pages.
- **Subprocess safety**: queries and arguments are passed as process arguments without
  shell execution (`shell=False`).
- **Local cache**: if the cache directory cannot be created, caching is disabled. The
  process never falls back to a shared `/tmp` database. New cache files are mode `0600`.

### What is Out of Scope (Caller Responsibility)

The following are deliberately **not** defended by Josty and are the agent runtime's job:

- **DNS rebinding.** DNS is resolved during validation and again at connect time, with no
  pinning or custom transport. An attacker who can observe a public name resolve to a safe
  address, then flip the answer to a loopback / link-local address between hops, can still
  reach a private target. Any agent embedding this in a tool environment must add an egress
  policy (e.g. `nftables`, eBPF, or a sandboxed network namespace) that denies
  private/loopback/link-local destinations at the network layer and/or uses a vetted HTTP
  client that pins the validated address across the redirect chain.
- **Prompt injection in fetched content.** Text extracted from web pages is untrusted data.
  The agent runtime must treat search snippets and fetched markdown as data, never as
  system instructions or tool input.
- **Upstream backend trust.** Search results reflect public web rankings and may contain
  malicious, spam, or misleading URLs.
- **Rate limiting / IP reputation.** Upstream providers may throttle or block datacenter
  IPs. Josty does not implement proxy rotation or CAPTCHA solving.
- **Multi-tenant isolation, authenticated egress, quotas, and monitoring.** These belong
  to any third-party service that wraps Josty, not to the library.
