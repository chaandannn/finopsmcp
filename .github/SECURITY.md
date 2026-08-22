# Security Policy

## Supported Versions

| Version | Supported |
|---|---|
| 0.8.x (latest) | Yes |
| < 0.8.0 | No |

## Reporting a Vulnerability

Please do not report security vulnerabilities through public GitHub issues.

Email **chandan@nable.sh** with:
- A description of the vulnerability
- Steps to reproduce
- Potential impact
- Any suggested fixes

You will receive a response within 48 hours. If the issue is confirmed, we will release a patch as soon as possible and credit you in the release notes (unless you prefer to remain anonymous).

## Security Model

- Credentials are encrypted with Fernet (AES-128-CBC) and stored in your OS keyring (macOS Keychain, Windows Credential Manager, libsecret on Linux)
- Credentials never leave your machine
- All cloud provider API calls are read-only by default
- License keys are signed with HMAC-SHA256
- No telemetry contains credentials or cost data -- only anonymous install IDs and event names
