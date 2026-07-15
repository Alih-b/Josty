# Security policy

Report vulnerabilities privately through the repository's **Security → Report a vulnerability** form.
Do not open a public issue for an undisclosed vulnerability.

Deep Search is a local command and Python library, not an authenticated network service. Search
queries leave the machine for selected upstream engines and, only when explicitly enabled, GitHub.
Fetched content is untrusted input and must never be treated as agent instructions.

The fetcher restricts URLs to public HTTP(S), rejects credential-bearing URLs, revalidates redirects,
checks resolved addresses, and bounds decoded downloads and extracted text. DNS is resolved during
validation and again during connection, so deliberate DNS rebinding remains a known residual. Any
third-party service wrapper must add network-level egress policy, authentication, quotas, isolation,
and monitoring.
