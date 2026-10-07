"""
Federation authentication manager.

Singleton class for managing OAuth2 client credentials authentication
for peer registry federation. Handles token caching and automatic refresh.
"""

import logging
import os
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


# Constants
TOKEN_REFRESH_BUFFER_SECONDS: int = 60
DEFAULT_TOKEN_TIMEOUT_SECONDS: int = 30


class FederationAuthManager:
    """
    Singleton authentication manager for federation clients.

    Handles OAuth2 client credentials flow with token caching and
    expiry-aware refresh. Thread-safe for concurrent access.
    """

    _instance: Optional["FederationAuthManager"] = None
    _lock: Lock = Lock()

    def __new__(cls) -> "FederationAuthManager":
        """Create singleton instance."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        """Initialize authentication manager."""
        if self._initialized:
            return

        self._initialized = True
        self._access_token: str | None = None
        self._token_expiry: datetime | None = None
        self._token_lock = Lock()

        # Get configuration from environment
        self._token_endpoint = os.getenv("FEDERATION_TOKEN_ENDPOINT")
        self._client_id = os.getenv("FEDERATION_CLIENT_ID")
        self._client_secret = os.getenv("FEDERATION_CLIENT_SECRET")

        # Validate configuration at startup
        self._validate_config()

        # HTTP client for token requests
        self._http_client = httpx.Client(timeout=DEFAULT_TOKEN_TIMEOUT_SECONDS)

        logger.info("FederationAuthManager initialized")

    def _validate_config(self) -> None:
        """
        Validate required environment variables are present.

        Logs clear warnings if configuration is missing but doesn't
        raise exceptions (to allow registry to start without federation).
        """
        missing = []

        if not self._token_endpoint:
            missing.append("FEDERATION_TOKEN_ENDPOINT")
        if not self._client_id:
            missing.append("FEDERATION_CLIENT_ID")
        if not self._client_secret:
            missing.append("FEDERATION_CLIENT_SECRET")

        if missing:
            logger.warning(
                f"Federation authentication not configured. Missing environment variables: {', '.join(missing)}"
            )
            logger.warning(
                "Peer registry federation will not be available until these variables are set."
            )
            logger.info("To enable federation, set the following environment variables:")
            for var in missing:
                logger.info(f"  - {var}")
        else:
            logger.info(
                f"Federation authentication configured. Token endpoint: {self._token_endpoint}"
            )

    def is_configured(self) -> bool:
        """
        Check if federation authentication is properly configured.

        Returns:
            True if all OAuth2 variables are set
        """
        return all(
            [
                self._token_endpoint,
                self._client_id,
                self._client_secret,
            ]
        )

    def get_token(self) -> str | None:
        """
        Get valid access token for federation API calls.

        Returns cached OAuth2 token if still valid (with 60s buffer),
        or requests a new token via client credentials flow.

        Returns:
            Access token or None if authentication fails

        Raises:
            ValueError: If federation authentication is not configured
        """
        if not self.is_configured():
            raise ValueError(
                "Federation authentication not configured. "
                "Set FEDERATION_TOKEN_ENDPOINT, FEDERATION_CLIENT_ID, "
                "and FEDERATION_CLIENT_SECRET environment variables."
            )

        with self._token_lock:
            # Check if cached token is still valid
            if self._is_token_valid():
                logger.debug("Using cached access token")
                return self._access_token

            # Request new token
            logger.info("Requesting new access token for federation")
            return self._refresh_token()

    def _is_token_valid(self) -> bool:
        """
        Check if cached token is still valid.

        Returns:
            True if token exists and hasn't expired (with buffer)
        """
        if not self._access_token or not self._token_expiry:
            return False

        # Check if token expires within buffer period
        now = datetime.now(UTC)
        buffer_time = self._token_expiry - timedelta(seconds=TOKEN_REFRESH_BUFFER_SECONDS)

        return now < buffer_time

    def _refresh_token(self) -> str | None:
        """
        Request new access token via OAuth2 client credentials flow.

        Returns:
            Access token or None if request fails
        """
        try:
            # Build token request
            headers = {
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            }

            data = {
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            }

            logger.debug(f"Requesting token from {self._token_endpoint}")

            # Make token request
            response = self._http_client.post(
                self._token_endpoint,
                data=data,
                headers=headers,
            )

            response.raise_for_status()
            token_data = response.json()

            # Extract token and expiry
            self._access_token = token_data.get("access_token")
            expires_in = token_data.get("expires_in", 3600)

            if not self._access_token:
                logger.error("Token response missing access_token field")
                return None

            # Set expiry time
            self._token_expiry = datetime.now(UTC) + timedelta(seconds=expires_in)

            logger.info(f"Successfully obtained access token (expires in {expires_in}s)")
            return self._access_token

        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error obtaining access token: {e.response.status_code} - {e}")
            if e.response.status_code in [401, 403]:
                logger.error(
                    "Authentication failed. Check FEDERATION_CLIENT_ID and "
                    "FEDERATION_CLIENT_SECRET are correct."
                )
            return None

        except httpx.RequestError as e:
            logger.error(f"Network error obtaining access token: {e}")
            logger.error(f"Token endpoint: {self._token_endpoint}")
            return None

        except Exception as e:
            logger.error(f"Unexpected error obtaining access token: {e}")
            return None

    def clear_token(self) -> None:
        """
        Clear cached token.

        Useful for forcing a token refresh or clearing expired tokens.
        """
        with self._token_lock:
            self._access_token = None
            self._token_expiry = None
            logger.info("Cleared cached access token")

    def __del__(self):
        """Clean up HTTP client on deletion."""
        if hasattr(self, "_http_client"):
            self._http_client.close()
