# Security Policy

## Supported versions

Only the latest release receives fixes.

| Version | Supported |
| ------- | --------- |
| 1.3.x   | Yes       |

## Reporting a vulnerability

Please report security issues privately through GitHub:
**Security** tab of this repository → **Report a vulnerability**.
Do not open a public issue for security problems.

You can expect a first reply within a week.

## Deployment notes

- The integration logs in to the RutOS WebUI API with the credentials you enter. They
  are stored in the Home Assistant config entry (and in backups). Create a dedicated user
  with read-only rights for Home Assistant instead of using `admin`, and use encrypted
  backups.
- Credentials are only sent over HTTPS to a certificate that is pinned by its SHA-256
  fingerprint at setup, or verified by a trusted CA if you turn on certificate
  verification. Plain HTTP is used only when you enter an `http://` address yourself, and
  then a warning is shown in Repairs.
- A changed certificate stops the integration from sending credentials and raises a
  Repairs issue. Accept a legitimately new certificate with **Reconfigure**.
- The integration opens no listening ports.
- NTP itself (UDP port 123) is unauthenticated unless NTP authentication is enabled on
  the clients. Keep the NTP server on a trusted network segment; see the network advice
  in the README.
- Diagnostics downloads redact the username, password, address, certificate fingerprint,
  serial number and MAC addresses.
