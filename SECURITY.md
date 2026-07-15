# Security policy

Report vulnerabilities privately through the repository's **Security → Report a vulnerability** form. Do not open a public issue for an undisclosed vulnerability. The maintainer will acknowledge a report, coordinate remediation, and publish an advisory when appropriate.

The HTTP API is intended for a trusted local user and should remain bound to localhost. URL validation and bounded downloads reduce risk but do not replace authentication, network isolation, egress controls, quotas, and monitoring. DNS is resolved separately during URL validation and the subsequent connection, so network-level egress policy is required before serving untrusted clients. Retrieved web content is untrusted input and must never be treated as agent instructions.
