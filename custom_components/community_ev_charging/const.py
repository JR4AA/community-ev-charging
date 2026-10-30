"""Constants for Community EV Charging."""
from datetime import timedelta

DOMAIN = "community_ev_charging"
CONF_TOKEN = "token"
SCAN_INTERVAL = timedelta(seconds=30)

# OCPP 1.6 connector statuses, plus the server's own "Offline" and "Unknown".
SOCKET_STATES = [
    "available", "preparing", "charging", "suspendedev", "suspendedevse",
    "finishing", "reserved", "unavailable", "faulted", "offline", "unknown",
]
