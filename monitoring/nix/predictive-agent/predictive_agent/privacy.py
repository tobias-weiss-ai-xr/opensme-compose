"""Outbound privacy scrubber for the predictive-agent.

Before anything is sent to an external LLM (tud/saia/openai), sensitive
material must be removed: secrets/API keys, RFC1918 private IPs, internal
openSME hostnames, absolute user paths. Local Ollama analysis is exempt.

Contract: contracts/agents.yml -> privacy.strip =
  [secrets, private_ips, internal_hostnames, user_paths]
"""

import re

REDACTED = "[REDACTED]"

# key=value / key: value pairs carrying credentials
_SECRET_RE = re.compile(
    r"(?:password|passwd|pass|pwd|secret|token|api[_-]?key|apikey|auth"
    r"|private[_-]?key|access[_-]?key|accesskey|credential)"
    r"\s*[=:]\s*[A-Za-z0-9+/=_.-]{8,}",
    re.IGNORECASE,
)

# RFC1918, link-local, loopback, this-network
_PRIVATE_IP_RE = re.compile(
    r"\b(?:0\.\d{1,3}\.\d{1,3}\.\d{1,3}|"
    r"10\.(?:\d{1,3}\.){2}\d{1,3}|"
    r"172\.(?:1[6-9]|2\d|3[0-1])\.(?:\d{1,3}\.)\d{1,3}|"
    r"192\.168\.(?:\d{1,3}\.)\d{1,3}|"
    r"169\.254\.(?:\d{1,3}\.)\d{1,3}|"
    r"127\.(?:\d{1,3}\.){2}\d{1,3})\b"
)

# internal hostnames / compose service names
_INTERNAL_HOST_RE = re.compile(
    r"\b(?:opensme|opendesk-sme|sogo6?|paperless|zitadel|stalwart|postgres|redis|"
    r"traefik|kalender|opencloud|invoice-ninja|dev-agent|dev-maintenance-bot|"
    r"predictive-agent|taskfleet|ollama|pgbouncer|gotenberg|collabora|memcached)"
    r"(?:\.local|\.internal|\.compose|\.opensme|\.docker|\.)?\b",
    re.IGNORECASE,
)

# absolute user paths
_USER_PATH_RE = re.compile(r"/(?:home|Users|root)/[\w][\w-]*/[^\s\"']*")


def scrub(text: str) -> str:
    """Redact sensitive material from a string destined for an external LLM."""
    if not text:
        return text
    out = _SECRET_RE.sub(REDACTED, text)
    out = _PRIVATE_IP_RE.sub(REDACTED, out)
    out = _INTERNAL_HOST_RE.sub(REDACTED, out)
    out = _USER_PATH_RE.sub(REDACTED, out)
    return out


def scrub_context(data: dict) -> dict:
    """Scrub every string value in a context dict (recursively)."""
    def walk(value):
        if isinstance(value, str):
            return scrub(value)
        if isinstance(value, list):
            return [walk(v) for v in value]
        if isinstance(value, dict):
            return {k: walk(v) for k, v in value.items()}
        return value
    return walk(data)
