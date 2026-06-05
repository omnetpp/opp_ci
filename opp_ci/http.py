"""
Shared HTTP helpers for the worker and Python client.

Centralizes TLS verification policy so a single set of env vars
(`OPP_CI_TLS_CA_BUNDLE`, `OPP_CI_TLS_INSECURE`) governs every outbound
HTTPS call we make to a coordinator that doesn't present a publicly
trusted certificate (self-signed, Cloudflare Origin Certificate,
internal CA).
"""

import logging

from opp_ci import config as cfg

_logger = logging.getLogger(__name__)

_warned_insecure = False


def configure_session(session, *, ca_bundle=None, insecure=None):
    """Apply TLS verification policy to a `requests.Session`.

    Resolution order for each setting:
      explicit kwarg → config (env var) → requests default.

    `insecure=True` overrides any `ca_bundle`. Logs a one-shot WARNING
    per process the first time verification is disabled.
    """
    global _warned_insecure

    if insecure is None:
        insecure = cfg.TLS_INSECURE
    if ca_bundle is None:
        ca_bundle = cfg.TLS_CA_BUNDLE

    if insecure:
        session.verify = False
        if not _warned_insecure:
            _logger.warning(
                "TLS verification disabled (OPP_CI_TLS_INSECURE=1) — "
                "never use this in production."
            )
            _warned_insecure = True
        # Silence the per-request urllib3 warning once at process start.
        try:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        except ImportError:
            pass
    elif ca_bundle:
        session.verify = ca_bundle
    # else: leave default (system CA store via certifi)
    return session
