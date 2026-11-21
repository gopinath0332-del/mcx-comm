"""
src/kite_auth.py
================
Thin wrapper around Kite Connect authentication.
Reuses the same pattern as nifty-strike-engine/src/kite_auth.py.
The access token is managed by nifty-strike-engine — we only consume it.
"""

from __future__ import annotations

import logging

from kiteconnect import KiteConnect

logger = logging.getLogger(__name__)


def build_authenticated_kite(api_key: str, access_token: str) -> KiteConnect:
    """Return an authenticated KiteConnect instance ready for API calls."""
    if not api_key or not access_token:
        raise ValueError(
            "KITE_API_KEY and KITE_ACCESS_TOKEN must be set.\n"
            "The access token is shared from nifty-strike-engine — make sure "
            "NSE_ENV_PATH in .env points to the correct nifty-strike-engine .env file."
        )
    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)
    logger.info("Kite Connect client authenticated (api_key=%s…)", api_key[:6])
    return kite
