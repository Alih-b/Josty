# Security policy

Report vulnerabilities privately through the repository's **Security → Report a vulnerability** form.
Do not open a public issue for an undisclosed vulnerability.

Deep Search is a local command and Python library, not an authenticated network service. Search
queries leave the machine for selected upstream engines and, only when explicitly enabled, GitHub.
Fetched content is untrusted input and must never be treated as agent instructions.

### Fetch SSRF guard — scope

The fetcher is a `ddgs` wrapper, not a generic URL fetcher. Its SSRF guard covers the threat model
Deep Search is responsible for and nothing more:

- **Scheme:** only `http://` and `https://` are accepted. `file:`, `ftp:`, `gopher:`, custom schemes,
  and scheme-less URLs are rejected before any DNS lookup.
- **Credentials:** URLs containing a `user:password@` component are rejected before resolution.
- **Resolved-address check:** every address returned by `getaddrinfo` is checked with
  `ipaddress.ip_address(...).is_global`. The check rejects loopback (`127.0.0.0/8`, `::1/128`),
  link-local (`169.254.0.0/16`, `fe80::/10`, including cloud metadata at `169.254.169.254`),
  private (`10/8`, `172.16/12`, `192.168/16`, `fc00::/7`), and the rest of the IANA-special-purpose
  range (`0.0.0.0/8`, `100.64/10` CGNAT, `192.0.0.0/24`, `192.0.2.0/24`, `198.18/15` benchmark,
  `198.51.100.0/24`, `203.0.113.0/24`, `224/4` multicast, `240/4` reserved, `::/128`, `::ffff:0:0/96`,
  `2001:db8::/32`, multicast `ff00::/8`). IPv4-mapped IPv6 addresses are also rejected.
- **Redirects:** the same guard runs on every hop, with a six-hop cap, so a redirect into a
  blocked range is caught.
- **Response bounds:** `Content-Type` must be `text/html`, `application/xhtml+xml`, or `text/plain`;
  the decoded body is capped at `max_download_bytes`; the extracted text is capped at
  `max_content_chars`.

### Out of scope — caller's responsibility

The following are deliberately **not** defended by Deep Search and are the agent runtime's job:

- **DNS rebinding.** DNS is resolved during validation and again at connect time, with no pinning
  or custom transport. An attacker who can observe a public name resolve to a safe address, then flip
  the answer to a loopback / link-local address between hops, can still reach a private target. Any
  agent embedding this in a tool environment must add an egress policy (e.g. `nftables`, eBPF, or a
  sandboxed network namespace) that denies private/loopback/link-local destinations at the network
  layer and/or uses a vetted HTTP client that pins the validated address across the redirect chain.
- **Authenticated egress, quotas, multi-tenant isolation, and monitoring.** These belong to the
  third-party service that wraps Deep Search, not to the library.
- **Untrusted content as instructions.** Fetched pages are returned as data; the agent runtime is
  responsible for treating them as untrusted and never as tool input or model instructions.
