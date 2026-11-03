"""Constants for the Teltonika NTP Server integration."""

from datetime import timedelta
from typing import Final

DOMAIN: Final = "teltonika_ntp"
MANUFACTURER: Final = "Teltonika"

# How often the RutOS API and the NTP service are polled. NTP servers rate limit
# clients that ask too often (ntpd answers with a "RATE" kiss-o'-death packet), so
# keep this well above the 8 second average interval ntpd allows by default.
SCAN_INTERVAL: Final = timedelta(seconds=60)

# When the server answers with a kiss-o'-death code (RATE, DENY, RSTR) the poll
# interval doubles up to this maximum, and returns to SCAN_INTERVAL on a normal reply.
MAX_SCAN_INTERVAL: Final = timedelta(minutes=15)
BACKOFF_KISS_CODES: Final = frozenset({"RATE", "DENY", "RSTR"})

# Config entry key for the pinned SHA-256 fingerprint of the device certificate.
CONF_FINGERPRINT: Final = "certificate_fingerprint"

# Repair issue IDs (suffixed with the config entry ID).
ISSUE_CERTIFICATE_MISMATCH: Final = "certificate_mismatch"
ISSUE_INSECURE_HTTP: Final = "insecure_http"

NTP_PORT: Final = 123
NTP_TIMEOUT: Final = 5.0
API_TIMEOUT: Final = 10.0

# Model prefixes this integration is written for. DHCP discovery ignores other
# Teltonika devices so it does not compete with the core "teltonika" integration.
SUPPORTED_MODEL_PREFIXES: Final = ("NTP",)

# Synchronization states reported by the "Synchronization" sensor.
SYNC_SYNCHRONIZED: Final = "synchronized"
SYNC_UNSYNCHRONIZED: Final = "unsynchronized"
SYNC_RATE_LIMITED: Final = "rate_limited"
SYNC_NO_RESPONSE: Final = "no_response"
SYNC_STATES: Final = [
    SYNC_SYNCHRONIZED,
    SYNC_UNSYNCHRONIZED,
    SYNC_RATE_LIMITED,
    SYNC_NO_RESPONSE,
]
