# Security Policy

## Supported Versions

| Version | Supported |
| --- | --- |
| `0.1.x` / `main` | Yes |

## Scope

Security and data-safety reports are especially welcome for issues such as:

- Bypassing `allow_libraries` write boundaries or `*.cdslck` / `*.lck` live edit lock guards
- Path traversal or symlink escape in snapshot loading, spool IPC, or atomic draft-and-publish restore
- Command or SKILL injection in `request.il` rendering, `cds.lib` parsing, or SSH transport
- Accidental credential exposure in `.oagit/config.json` or `.gitignore` templates

## Reporting a Vulnerability

Please **do not** disclose unpatched security vulnerabilities or proprietary PDK/design data in public issues.

Instead, report vulnerabilities via GitHub's **Security -> Advisories -> Report a vulnerability** feature on this repository (or contact the maintainer directly via GitHub). Include reproduction steps and affected versions; we will acknowledge and coordinate a fix promptly.
