"""
Unit tests for FederationAuthManager.

Tests OAuth2 client credentials authentication including token caching,
expiry handling, and error scenarios.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, Mock, patch

import httpx
import pytest

from registry.services.federation.federation_auth import (
    FederationAuthManager,
)


@pytest.fixture
def auth_env_vars(
    monkeypatch,
):
    """Set up environment variables for authentication."""
    monkeypatch.setenv("FEDERATION_TOKEN_ENDPOINT", "https://auth.example.com/token")
    monkeypatch.setenv("FEDERATION_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("FEDERATION_CLIENT_SECRET", "test-client-secret")


@pytest.fixture
def missing_env_vars(
    monkeypatch,
):
    """Remove authentication environment variables."""
    monkeypatch.delenv("FEDERATION_TOKEN_ENDPOINT", raising=False)
    monkeypatch.delenv("FEDERATION_CLIENT_ID", raising=False)
    monkeypatch.delenv("FEDERATION_CLIENT_SECRET", raising=False)


@pytest.fixture
def mock_http_client():
    """Create a mock HTTP client for token requests."""
    with patch("registry.services.federation.federation_auth.httpx.Client") as mock:
        client_instance = MagicMock()
        mock.return_value = client_instance
        yield client_instance


@pytest.fixture
def clear_singleton():
    """Clear singleton instance before each test."""
    # Reset the singleton instance
    FederationAuthManager._instance = None
    yield
    # Clean up after test
    FederationAuthManager._instance = None


class TestFederationAuthManagerSingleton:
    """Test singleton pattern implementation."""

    def test_singleton_same_instance(
        self,
        auth_env_vars,
        clear_singleton,
        mock_http_client,
    ):
        """Test that FederationAuthManager returns the same instance."""
        # Arrange & Act
        instance1 = FederationAuthManager()
        instance2 = FederationAuthManager()

        # Assert
        assert instance1 is instance2

    def test_singleton_initialization_once(
        self,
        auth_env_vars,
        clear_singleton,
        mock_http_client,
    ):
        """Test that initialization only happens once."""
        # Arrange & Act
        instance1 = FederationAuthManager()
        instance1._test_marker = "initialized"

        instance2 = FederationAuthManager()

        # Assert
        assert hasattr(instance2, "_test_marker")
        assert instance2._test_marker == "initialized"


class TestFederationAuthManagerConfiguration:
    """Test configuration validation and setup."""

    def test_is_configured_with_all_env_vars(
        self,
        auth_env_vars,
        clear_singleton,
        mock_http_client,
    ):
        """Test is_configured returns True when all env vars are set."""
        # Arrange
        auth_manager = FederationAuthManager()

        # Act
        is_configured = auth_manager.is_configured()

        # Assert
        assert is_configured is True

    def test_is_configured_missing_token_endpoint(
        self,
        monkeypatch,
        clear_singleton,
        mock_http_client,
    ):
        """Test is_configured returns False when token endpoint is missing."""
        # Arrange
        monkeypatch.setenv("FEDERATION_CLIENT_ID", "test-client-id")
        monkeypatch.setenv("FEDERATION_CLIENT_SECRET", "test-client-secret")
        auth_manager = FederationAuthManager()

        # Act
        is_configured = auth_manager.is_configured()

        # Assert
        assert is_configured is False

    def test_is_configured_missing_client_id(
        self,
        monkeypatch,
        clear_singleton,
        mock_http_client,
    ):
        """Test is_configured returns False when client ID is missing."""
        # Arrange
        monkeypatch.setenv("FEDERATION_TOKEN_ENDPOINT", "https://auth.example.com/token")
        monkeypatch.setenv("FEDERATION_CLIENT_SECRET", "test-client-secret")
        auth_manager = FederationAuthManager()

        # Act
        is_configured = auth_manager.is_configured()

        # Assert
        assert is_configured is False

    def test_is_configured_missing_client_secret(
        self,
        monkeypatch,
        clear_singleton,
        mock_http_client,
    ):
        """Test is_configured returns False when client secret is missing."""
        # Arrange
        monkeypatch.setenv("FEDERATION_TOKEN_ENDPOINT", "https://auth.example.com/token")
        monkeypatch.setenv("FEDERATION_CLIENT_ID", "test-client-id")
        auth_manager = FederationAuthManager()

        # Act
        is_configured = auth_manager.is_configured()

        # Assert
        assert is_configured is False

    def test_missing_env_vars_logged_at_startup(
        self,
        missing_env_vars,
        clear_singleton,
        mock_http_client,
        caplog,
    ):
        """Test that missing env vars are logged clearly at startup."""
        # Arrange & Act
        auth_manager = FederationAuthManager()

        # Assert
        assert "Federation authentication not configured" in caplog.text
        assert "FEDERATION_TOKEN_ENDPOINT" in caplog.text
        assert "FEDERATION_CLIENT_ID" in caplog.text
        assert "FEDERATION_CLIENT_SECRET" in caplog.text

    def test_configured_env_vars_logged_at_startup(
        self,
        auth_env_vars,
        clear_singleton,
        mock_http_client,
        caplog,
    ):
        """Test that configuration is logged at startup."""
        # Arrange & Act
        import logging

        caplog.set_level(logging.INFO)
        auth_manager = FederationAuthManager()

        # Assert
        assert "Federation authentication configured" in caplog.text
        assert "https://auth.example.com/token" in caplog.text


class TestFederationAuthManagerTokenRequest:
    """Test token request and caching behavior."""

    def test_get_token_obtains_jwt_using_credentials(
        self,
        auth_env_vars,
        clear_singleton,
        mock_http_client,
    ):
        """Test that client obtains JWT using credentials from env vars."""
        # Arrange
        auth_manager = FederationAuthManager()
        mock_response = Mock()
        mock_response.json.return_value = {
            "access_token": "test-jwt-token",
            "expires_in": 3600,
        }
        mock_http_client.post.return_value = mock_response

        # Act
        token = auth_manager.get_token()

        # Assert
        assert token == "test-jwt-token"
        mock_http_client.post.assert_called_once()
        call_args = mock_http_client.post.call_args

        # Verify correct endpoint
        assert call_args[0][0] == "https://auth.example.com/token"

        # Verify correct data
        data = call_args[1]["data"]
        assert data["grant_type"] == "client_credentials"
        assert data["client_id"] == "test-client-id"
        assert data["client_secret"] == "test-client-secret"

    def test_get_token_raises_when_not_configured(
        self,
        missing_env_vars,
        clear_singleton,
        mock_http_client,
    ):
        """Test get_token raises ValueError when not configured."""
        # Arrange
        auth_manager = FederationAuthManager()

        # Act & Assert
        with pytest.raises(ValueError, match="Federation authentication not configured"):
            auth_manager.get_token()

    def test_token_is_cached_and_reused(
        self,
        auth_env_vars,
        clear_singleton,
        mock_http_client,
    ):
        """Test that token is cached and reused until near expiry."""
        # Arrange
        auth_manager = FederationAuthManager()
        mock_response = Mock()
        mock_response.json.return_value = {
            "access_token": "test-jwt-token",
            "expires_in": 3600,
        }
        mock_http_client.post.return_value = mock_response

        # Act - First request
        token1 = auth_manager.get_token()

        # Act - Second request (should use cache)
        token2 = auth_manager.get_token()

        # Assert
        assert token1 == token2
        assert token1 == "test-jwt-token"
        # Should only make one HTTP request
        assert mock_http_client.post.call_count == 1

    def test_expired_token_triggers_automatic_refresh(
        self,
        auth_env_vars,
        clear_singleton,
        mock_http_client,
    ):
        """Test that expired token triggers automatic refresh."""
        # Arrange
        auth_manager = FederationAuthManager()
        mock_response1 = Mock()
        mock_response1.json.return_value = {
            "access_token": "first-token",
            "expires_in": 1,  # Expires very soon
        }
        mock_response2 = Mock()
        mock_response2.json.return_value = {
            "access_token": "second-token",
            "expires_in": 3600,
        }
        mock_http_client.post.side_effect = [mock_response1, mock_response2]

        # Act - First request
        token1 = auth_manager.get_token()

        # Manually expire the token by setting expiry in the past
        auth_manager._token_expiry = datetime.now(UTC) - timedelta(seconds=1)

        # Act - Second request (should refresh)
        token2 = auth_manager.get_token()

        # Assert
        assert token1 == "first-token"
        assert token2 == "second-token"
        assert mock_http_client.post.call_count == 2

    def test_token_refresh_with_60s_buffer(
        self,
        auth_env_vars,
        clear_singleton,
        mock_http_client,
    ):
        """Test that token is refreshed with 60s buffer before expiry."""
        # Arrange
        auth_manager = FederationAuthManager()
        mock_response1 = Mock()
        mock_response1.json.return_value = {
            "access_token": "first-token",
            "expires_in": 3600,
        }
        mock_response2 = Mock()
        mock_response2.json.return_value = {
            "access_token": "second-token",
            "expires_in": 3600,
        }
        mock_http_client.post.side_effect = [mock_response1, mock_response2]

        # Act - First request
        token1 = auth_manager.get_token()

        # Set token expiry to 30 seconds from now (within buffer)
        auth_manager._token_expiry = datetime.now(UTC) + timedelta(seconds=30)

        # Act - Second request (should refresh due to buffer)
        token2 = auth_manager.get_token()

        # Assert
        assert token1 == "first-token"
        assert token2 == "second-token"
        assert mock_http_client.post.call_count == 2

    def test_token_not_refreshed_outside_buffer(
        self,
        auth_env_vars,
        clear_singleton,
        mock_http_client,
    ):
        """Test that token is not refreshed outside 60s buffer."""
        # Arrange
        auth_manager = FederationAuthManager()
        mock_response = Mock()
        mock_response.json.return_value = {
            "access_token": "test-token",
            "expires_in": 3600,
        }
        mock_http_client.post.return_value = mock_response

        # Act - First request
        token1 = auth_manager.get_token()

        # Set token expiry to 120 seconds from now (outside buffer)
        auth_manager._token_expiry = datetime.now(UTC) + timedelta(seconds=120)

        # Act - Second request (should use cache)
        token2 = auth_manager.get_token()

        # Assert
        assert token1 == token2
        assert token1 == "test-token"
        # Should only make one HTTP request
        assert mock_http_client.post.call_count == 1


class TestFederationAuthManagerErrorHandling:
    """Test error handling for various failure scenarios."""

    def test_http_401_error_handled_gracefully(
        self,
        auth_env_vars,
        clear_singleton,
        mock_http_client,
        caplog,
    ):
        """Test that HTTP 401 errors are handled gracefully."""
        # Arrange
        auth_manager = FederationAuthManager()
        mock_response = Mock()
        mock_response.status_code = 401
        mock_http_client.post.side_effect = httpx.HTTPStatusError(
            "Unauthorized",
            request=Mock(),
            response=mock_response,
        )

        # Act
        token = auth_manager.get_token()

        # Assert
        assert token is None
        assert "HTTP error obtaining access token: 401" in caplog.text
        assert "Authentication failed" in caplog.text
        assert "FEDERATION_CLIENT_ID" in caplog.text

    def test_http_403_error_handled_gracefully(
        self,
        auth_env_vars,
        clear_singleton,
        mock_http_client,
        caplog,
    ):
        """Test that HTTP 403 errors are handled gracefully."""
        # Arrange
        auth_manager = FederationAuthManager()
        mock_response = Mock()
        mock_response.status_code = 403
        mock_http_client.post.side_effect = httpx.HTTPStatusError(
            "Forbidden",
            request=Mock(),
            response=mock_response,
        )

        # Act
        token = auth_manager.get_token()

        # Assert
        assert token is None
        assert "HTTP error obtaining access token: 403" in caplog.text
        assert "Authentication failed" in caplog.text

    def test_http_500_error_handled_gracefully(
        self,
        auth_env_vars,
        clear_singleton,
        mock_http_client,
        caplog,
    ):
        """Test that HTTP 500 errors are handled gracefully."""
        # Arrange
        auth_manager = FederationAuthManager()
        mock_response = Mock()
        mock_response.status_code = 500
        mock_http_client.post.side_effect = httpx.HTTPStatusError(
            "Internal Server Error",
            request=Mock(),
            response=mock_response,
        )

        # Act
        token = auth_manager.get_token()

        # Assert
        assert token is None
        assert "HTTP error obtaining access token: 500" in caplog.text

    def test_network_timeout_handled_gracefully(
        self,
        auth_env_vars,
        clear_singleton,
        mock_http_client,
        caplog,
    ):
        """Test that network timeouts are handled gracefully."""
        # Arrange
        auth_manager = FederationAuthManager()
        mock_http_client.post.side_effect = httpx.TimeoutException("Request timed out")

        # Act
        token = auth_manager.get_token()

        # Assert
        assert token is None
        assert "Network error obtaining access token" in caplog.text

    def test_network_connection_error_handled_gracefully(
        self,
        auth_env_vars,
        clear_singleton,
        mock_http_client,
        caplog,
    ):
        """Test that network connection errors are handled gracefully."""
        # Arrange
        auth_manager = FederationAuthManager()
        mock_http_client.post.side_effect = httpx.ConnectError("Connection failed")

        # Act
        token = auth_manager.get_token()

        # Assert
        assert token is None
        assert "Network error obtaining access token" in caplog.text
        assert "https://auth.example.com/token" in caplog.text

    def test_missing_access_token_in_response(
        self,
        auth_env_vars,
        clear_singleton,
        mock_http_client,
        caplog,
    ):
        """Test handling of response missing access_token field."""
        # Arrange
        auth_manager = FederationAuthManager()
        mock_response = Mock()
        mock_response.json.return_value = {
            "expires_in": 3600,
            # access_token is missing
        }
        mock_http_client.post.return_value = mock_response

        # Act
        token = auth_manager.get_token()

        # Assert
        assert token is None
        assert "Token response missing access_token field" in caplog.text

    def test_unexpected_error_handled_gracefully(
        self,
        auth_env_vars,
        clear_singleton,
        mock_http_client,
        caplog,
    ):
        """Test that unexpected errors are handled gracefully."""
        # Arrange
        auth_manager = FederationAuthManager()
        mock_http_client.post.side_effect = Exception("Unexpected error")

        # Act
        token = auth_manager.get_token()

        # Assert
        assert token is None
        assert "Unexpected error obtaining access token" in caplog.text


class TestFederationAuthManagerClearToken:
    """Test token clearing functionality."""

    def test_clear_token_removes_cached_token(
        self,
        auth_env_vars,
        clear_singleton,
        mock_http_client,
    ):
        """Test that clear_token removes cached token."""
        # Arrange
        auth_manager = FederationAuthManager()
        mock_response = Mock()
        mock_response.json.return_value = {
            "access_token": "test-token",
            "expires_in": 3600,
        }
        mock_http_client.post.return_value = mock_response

        # Get a token
        token1 = auth_manager.get_token()
        assert token1 == "test-token"

        # Act - Clear the token
        auth_manager.clear_token()

        # Assert
        assert auth_manager._access_token is None
        assert auth_manager._token_expiry is None

    def test_clear_token_forces_refresh_on_next_get(
        self,
        auth_env_vars,
        clear_singleton,
        mock_http_client,
    ):
        """Test that clearing token forces refresh on next get."""
        # Arrange
        auth_manager = FederationAuthManager()
        mock_response1 = Mock()
        mock_response1.json.return_value = {
            "access_token": "first-token",
            "expires_in": 3600,
        }
        mock_response2 = Mock()
        mock_response2.json.return_value = {
            "access_token": "second-token",
            "expires_in": 3600,
        }
        mock_http_client.post.side_effect = [mock_response1, mock_response2]

        # Get first token
        token1 = auth_manager.get_token()

        # Act - Clear and get again
        auth_manager.clear_token()
        token2 = auth_manager.get_token()

        # Assert
        assert token1 == "first-token"
        assert token2 == "second-token"
        assert mock_http_client.post.call_count == 2
