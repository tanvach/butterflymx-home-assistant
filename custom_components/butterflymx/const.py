"""Constants for the ButterflyMX integration."""

from datetime import timedelta

DOMAIN = "butterflymx"
CONF_EMAIL = "email"
CONF_PASSWORD = "password"
CONF_TOKENS = "tokens"
CONF_CODE = "code"
CONF_RESEND = "resend"

# One small GraphQL request per tenant; ButterflyMX allows 350 a minute.
# Short enough that a new call shows up while the visitor is still there.
SCAN_INTERVAL = timedelta(seconds=30)
