"""Factory for creating authentication provider instances."""

import logging
import os

from .base import AuthProvider
from .logto import LogtoProvider

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s,p%(process)s,{%(filename)s:%(lineno)d},%(levelname)s,%(message)s",
)

logger = logging.getLogger(__name__)


def get_auth_provider(provider_type: str | None = None) -> AuthProvider:
    """Factory function to get the appropriate auth provider.

    Args:
        provider_type: Type of provider to create ('logto').
                      If None, uses AUTH_PROVIDER environment variable.

    Returns:
        AuthProvider instance configured for the specified provider

    Raises:
        ValueError: If provider type is unknown or required config is missing
    """
    provider_type = provider_type or os.environ.get("AUTH_PROVIDER", "logto")

    logger.info(f"Creating authentication provider: {provider_type}")

    if provider_type == "logto":
        return _create_logto_provider()
    else:
        raise ValueError(f"Unknown auth provider: {provider_type}")


def _create_logto_provider() -> LogtoProvider:
    """Create and configure Logto provider."""
    logto_url = os.environ.get("LOGTO_URL")
    logto_external_url = os.environ.get("LOGTO_EXTERNAL_URL") or logto_url
    client_id = os.environ.get("LOGTO_CLIENT_ID")
    client_secret = os.environ.get("LOGTO_CLIENT_SECRET")

    # Optional M2M configuration
    m2m_client_id = os.environ.get("LOGTO_M2M_CLIENT_ID")
    m2m_client_secret = os.environ.get("LOGTO_M2M_CLIENT_SECRET")
    m2m_resource = os.environ.get("LOGTO_M2M_RESOURCE")

    missing_vars = []
    if not logto_url:
        missing_vars.append("LOGTO_URL")
    if not client_id:
        missing_vars.append("LOGTO_CLIENT_ID")
    if not client_secret:
        missing_vars.append("LOGTO_CLIENT_SECRET")

    if missing_vars:
        raise ValueError(
            f"Missing required Logto configuration: {', '.join(missing_vars)}. "
            "Please set these environment variables."
        )

    logger.info(
        f"Initializing Logto provider at {logto_url} (external: {logto_external_url})"
    )
    return LogtoProvider(
        logto_url=logto_url,
        client_id=client_id,
        client_secret=client_secret,
        logto_external_url=logto_external_url,
        m2m_client_id=m2m_client_id,
        m2m_client_secret=m2m_client_secret,
        m2m_resource=m2m_resource,
    )
