# Security Policy

## Supported versions

The latest 0.x release receives security patches. Earlier versions do not.

## Reporting a vulnerability

Please email harry@hvass.dev with details. Do NOT open a public GitHub
issue for security-impacting bugs.

Expected response:

- Acknowledgement within 48 hours
- Patch + advisory within 14 days for high-severity issues
- Coordinated disclosure on a mutually-agreed schedule

## What counts

- Arbitrary file read/write triggered by tool input
- SSRF via URL manipulation that bypasses the `data.gov.au` host
  whitelist
- Cache injection that allows serving forged responses
- Path traversal in the bundled ZIP extractor

What doesn't count: rate-limit exhaustion against data.gov.au (data.gov.au's
operators are the right contact), or output-formatting choices that an
agent may find unhelpful (open a regular issue for those).
