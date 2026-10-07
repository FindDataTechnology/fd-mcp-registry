"""
Unit tests for peer federation schema models.

This module provides comprehensive tests for the peer-to-peer federation
Pydantic models including:
- PeerRegistryConfig: Configuration for peer registry connections
- SyncMetadata: Metadata for items synced from peer registries
- SyncHistoryEntry: Record of sync operations
- PeerSyncStatus: Current sync status for a peer registry
- SyncResult: Result of a sync operation
- FederationExportResponse: Response model for federation export API

Tests cover:
- Field validation (required fields, types, constraints)
- URL validation and normalization
- Peer ID validation (filename safety)
- Sync interval constraints
- Datetime serialization/deserialization
- Default values
- Edge cases (unicode, whitespace, invalid characters)
- JSON schema generation for OpenAPI
"""

import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from registry.schemas.peer_federation_schema import (
    DEFAULT_SYNC_INTERVAL_MINUTES,
    MAX_SYNC_HISTORY_ENTRIES,
    MAX_SYNC_INTERVAL_MINUTES,
    MIN_SYNC_INTERVAL_MINUTES,
    FederationExportResponse,
    PeerRegistryConfig,
    PeerSyncStatus,
    SyncHistoryEntry,
    SyncMetadata,
    SyncResult,
    _validate_endpoint_url,
    _validate_peer_id,
)

# =============================================================================
# Test Helper Functions
# =============================================================================


@pytest.mark.unit
class TestValidateEndpointUrl:
    """Tests for _validate_endpoint_url helper function."""

    def test_valid_http_url(self):
        """Valid HTTP URL should be accepted."""
        url = "http://registry.example.com"
        result = _validate_endpoint_url(url)
        assert result == url

    def test_valid_https_url(self):
        """Valid HTTPS URL should be accepted."""
        url = "https://registry.example.com"
        result = _validate_endpoint_url(url)
        assert result == url

    def test_trailing_slash_removed(self):
        """Trailing slash should be removed for consistency."""
        url = "https://registry.example.com/"
        result = _validate_endpoint_url(url)
        assert result == "https://registry.example.com"

    def test_multiple_trailing_slashes_removed(self):
        """Multiple trailing slashes should be removed."""
        url = "https://registry.example.com///"
        result = _validate_endpoint_url(url)
        assert result == "https://registry.example.com"

    def test_url_with_port(self):
        """URL with port should be valid."""
        url = "https://registry.example.com:8080"
        result = _validate_endpoint_url(url)
        assert result == url

    def test_url_with_path(self):
        """URL with path should be valid."""
        url = "https://registry.example.com/api/v1"
        result = _validate_endpoint_url(url)
        assert result == url

    def test_empty_url_rejected(self):
        """Empty URL should be rejected."""
        with pytest.raises(ValueError, match="Endpoint URL cannot be empty"):
            _validate_endpoint_url("")

    def test_missing_protocol_rejected(self):
        """URL without protocol should be rejected."""
        with pytest.raises(ValueError, match="must use HTTP or HTTPS protocol"):
            _validate_endpoint_url("registry.example.com")

    def test_invalid_protocol_rejected(self):
        """URL with invalid protocol should be rejected."""
        with pytest.raises(ValueError, match="must use HTTP or HTTPS protocol"):
            _validate_endpoint_url("ftp://registry.example.com")

    def test_missing_hostname_rejected(self):
        """URL without hostname should be rejected."""
        with pytest.raises(ValueError, match="must include a valid hostname"):
            _validate_endpoint_url("https://")

    def test_very_long_url(self):
        """Very long URL should be accepted if valid."""
        long_path = "/".join(["segment"] * 50)
        url = f"https://registry.example.com/{long_path}"
        result = _validate_endpoint_url(url)
        assert result == url


@pytest.mark.unit
class TestValidatePeerId:
    """Tests for _validate_peer_id helper function."""

    def test_valid_simple_peer_id(self):
        """Simple alphanumeric peer ID should be valid."""
        peer_id = "central-registry"
        result = _validate_peer_id(peer_id)
        assert result == peer_id

    def test_valid_peer_id_with_underscores(self):
        """Peer ID with underscores should be valid."""
        peer_id = "central_registry_prod"
        result = _validate_peer_id(peer_id)
        assert result == peer_id

    def test_valid_peer_id_with_dots(self):
        """Peer ID with dots should be valid."""
        peer_id = "registry.central.prod"
        result = _validate_peer_id(peer_id)
        assert result == peer_id

    def test_unicode_peer_id(self):
        """Peer ID with unicode characters should be valid."""
        peer_id = "registry-中文-test"
        result = _validate_peer_id(peer_id)
        assert result == peer_id

    def test_whitespace_trimmed(self):
        """Leading/trailing whitespace should be trimmed."""
        peer_id = "  central-registry  "
        result = _validate_peer_id(peer_id)
        assert result == "central-registry"

    def test_empty_string_rejected(self):
        """Empty string should be rejected."""
        with pytest.raises(ValueError, match="Peer ID cannot be empty"):
            _validate_peer_id("")

    def test_whitespace_only_rejected(self):
        """Whitespace-only string should be rejected."""
        with pytest.raises(ValueError, match="Peer ID cannot be whitespace only"):
            _validate_peer_id("   ")

    def test_forward_slash_rejected(self):
        """Forward slash should be rejected (invalid filename character)."""
        with pytest.raises(ValueError, match="cannot contain '/' character"):
            _validate_peer_id("central/registry")

    def test_backslash_rejected(self):
        """Backslash should be rejected (invalid filename character)."""
        with pytest.raises(ValueError, match="cannot contain"):
            _validate_peer_id("central\\registry")

    def test_colon_rejected(self):
        """Colon should be rejected (invalid filename character)."""
        with pytest.raises(ValueError, match="cannot contain ':' character"):
            _validate_peer_id("central:registry")

    def test_asterisk_rejected(self):
        """Asterisk should be rejected (invalid filename character)."""
        with pytest.raises(ValueError, match="cannot contain '\\*' character"):
            _validate_peer_id("central*registry")

    def test_question_mark_rejected(self):
        """Question mark should be rejected (invalid filename character)."""
        with pytest.raises(ValueError, match="cannot contain '\\?' character"):
            _validate_peer_id("central?registry")

    def test_quote_rejected(self):
        """Quote should be rejected (invalid filename character)."""
        with pytest.raises(ValueError, match="cannot contain '\"' character"):
            _validate_peer_id('central"registry')

    def test_less_than_rejected(self):
        """Less-than sign should be rejected (invalid filename character)."""
        with pytest.raises(ValueError, match="cannot contain '<' character"):
            _validate_peer_id("central<registry")

    def test_greater_than_rejected(self):
        """Greater-than sign should be rejected (invalid filename character)."""
        with pytest.raises(ValueError, match="cannot contain '>' character"):
            _validate_peer_id("central>registry")

    def test_pipe_rejected(self):
        """Pipe character should be rejected (invalid filename character)."""
        with pytest.raises(ValueError, match="cannot contain '\\|' character"):
            _validate_peer_id("central|registry")

    def test_max_length_accepted(self):
        """Peer ID at max length (255 chars) should be accepted."""
        peer_id = "a" * 255
        result = _validate_peer_id(peer_id)
        assert result == peer_id

    def test_exceeds_max_length_rejected(self):
        """Peer ID exceeding max length should be rejected."""
        peer_id = "a" * 256
        with pytest.raises(ValueError, match="cannot exceed 255 characters"):
            _validate_peer_id(peer_id)


# =============================================================================
# Test PeerRegistryConfig Model
# =============================================================================


@pytest.mark.unit
class TestPeerRegistryConfig:
    """Tests for PeerRegistryConfig model."""

    def test_valid_minimal_config(self):
        """Minimal valid configuration should be accepted."""
        # Arrange, Act
        config = PeerRegistryConfig(
            peer_id="central-registry",
            name="Central Registry",
            endpoint="https://registry.example.com",
        )

        # Assert
        assert config.peer_id == "central-registry"
        assert config.name == "Central Registry"
        assert config.endpoint == "https://registry.example.com"
        assert config.enabled is True
        assert config.sync_mode == "all"
        assert config.sync_interval_minutes == DEFAULT_SYNC_INTERVAL_MINUTES

    def test_valid_full_config(self):
        """Full configuration with all fields should be accepted."""
        # Arrange, Act
        now = datetime.now(UTC)
        config = PeerRegistryConfig(
            peer_id="team-registry",
            name="Team Registry",
            endpoint="https://team.registry.com",
            enabled=False,
            sync_mode="whitelist",
            whitelist_servers=["/server1", "/server2"],
            whitelist_agents=["/agent1"],
            tag_filters=["production"],
            sync_interval_minutes=120,
            created_at=now,
            updated_at=now,
        )

        # Assert
        assert config.peer_id == "team-registry"
        assert config.enabled is False
        assert config.sync_mode == "whitelist"
        assert config.whitelist_servers == ["/server1", "/server2"]
        assert config.whitelist_agents == ["/agent1"]
        assert config.sync_interval_minutes == 120

    def test_required_field_peer_id_missing(self):
        """Missing peer_id should raise validation error."""
        with pytest.raises(ValidationError) as exc_info:
            PeerRegistryConfig(
                name="Central Registry",
                endpoint="https://registry.example.com",
            )
        assert "peer_id" in str(exc_info.value)

    def test_required_field_name_missing(self):
        """Missing name should raise validation error."""
        with pytest.raises(ValidationError) as exc_info:
            PeerRegistryConfig(
                peer_id="central-registry",
                endpoint="https://registry.example.com",
            )
        assert "name" in str(exc_info.value)

    def test_required_field_endpoint_missing(self):
        """Missing endpoint should raise validation error."""
        with pytest.raises(ValidationError) as exc_info:
            PeerRegistryConfig(
                peer_id="central-registry",
                name="Central Registry",
            )
        assert "endpoint" in str(exc_info.value)

    def test_invalid_endpoint_url(self):
        """Invalid endpoint URL should raise validation error."""
        with pytest.raises(ValidationError) as exc_info:
            PeerRegistryConfig(
                peer_id="central-registry",
                name="Central Registry",
                endpoint="not-a-url",
            )
        assert "endpoint" in str(exc_info.value).lower()

    def test_endpoint_trailing_slash_removed(self):
        """Trailing slash in endpoint should be removed."""
        config = PeerRegistryConfig(
            peer_id="central-registry",
            name="Central Registry",
            endpoint="https://registry.example.com/",
        )
        assert config.endpoint == "https://registry.example.com"

    def test_sync_interval_minimum_enforced(self):
        """Sync interval below minimum should raise validation error."""
        with pytest.raises(ValidationError) as exc_info:
            PeerRegistryConfig(
                peer_id="central-registry",
                name="Central Registry",
                endpoint="https://registry.example.com",
                sync_interval_minutes=MIN_SYNC_INTERVAL_MINUTES - 1,
            )
        assert "sync_interval_minutes" in str(exc_info.value)

    def test_sync_interval_maximum_enforced(self):
        """Sync interval above maximum should raise validation error."""
        with pytest.raises(ValidationError) as exc_info:
            PeerRegistryConfig(
                peer_id="central-registry",
                name="Central Registry",
                endpoint="https://registry.example.com",
                sync_interval_minutes=MAX_SYNC_INTERVAL_MINUTES + 1,
            )
        assert "sync_interval_minutes" in str(exc_info.value)

    def test_sync_interval_at_minimum(self):
        """Sync interval at minimum should be accepted."""
        config = PeerRegistryConfig(
            peer_id="central-registry",
            name="Central Registry",
            endpoint="https://registry.example.com",
            sync_interval_minutes=MIN_SYNC_INTERVAL_MINUTES,
        )
        assert config.sync_interval_minutes == MIN_SYNC_INTERVAL_MINUTES

    def test_sync_interval_at_maximum(self):
        """Sync interval at maximum should be accepted."""
        config = PeerRegistryConfig(
            peer_id="central-registry",
            name="Central Registry",
            endpoint="https://registry.example.com",
            sync_interval_minutes=MAX_SYNC_INTERVAL_MINUTES,
        )
        assert config.sync_interval_minutes == MAX_SYNC_INTERVAL_MINUTES

    def test_invalid_sync_mode(self):
        """Invalid sync_mode should raise validation error."""
        with pytest.raises(ValidationError) as exc_info:
            PeerRegistryConfig(
                peer_id="central-registry",
                name="Central Registry",
                endpoint="https://registry.example.com",
                sync_mode="invalid",
            )
        assert "sync_mode" in str(exc_info.value)

    def test_sync_mode_all(self):
        """sync_mode 'all' should be valid."""
        config = PeerRegistryConfig(
            peer_id="central-registry",
            name="Central Registry",
            endpoint="https://registry.example.com",
            sync_mode="all",
        )
        assert config.sync_mode == "all"

    def test_sync_mode_whitelist(self):
        """sync_mode 'whitelist' should be valid."""
        config = PeerRegistryConfig(
            peer_id="central-registry",
            name="Central Registry",
            endpoint="https://registry.example.com",
            sync_mode="whitelist",
            whitelist_servers=["/server1"],
        )
        assert config.sync_mode == "whitelist"

    def test_sync_mode_tag_filter(self):
        """sync_mode 'tag_filter' should be valid."""
        config = PeerRegistryConfig(
            peer_id="central-registry",
            name="Central Registry",
            endpoint="https://registry.example.com",
            sync_mode="tag_filter",
            tag_filters=["production"],
        )
        assert config.sync_mode == "tag_filter"

    def test_whitelist_empty_list_accepted(self):
        """Empty whitelist should be accepted."""
        config = PeerRegistryConfig(
            peer_id="central-registry",
            name="Central Registry",
            endpoint="https://registry.example.com",
            sync_mode="whitelist",
            whitelist_servers=[],
            whitelist_agents=[],
        )
        assert config.whitelist_servers == []
        assert config.whitelist_agents == []

    def test_tag_filters_empty_list_accepted(self):
        """Empty tag_filters should be accepted."""
        config = PeerRegistryConfig(
            peer_id="central-registry",
            name="Central Registry",
            endpoint="https://registry.example.com",
            sync_mode="tag_filter",
            tag_filters=[],
        )
        assert config.tag_filters == []

    def test_peer_id_with_invalid_characters_rejected(self):
        """Peer ID with invalid filename characters should be rejected."""
        with pytest.raises(ValidationError):
            PeerRegistryConfig(
                peer_id="central/registry",
                name="Central Registry",
                endpoint="https://registry.example.com",
            )

    def test_peer_id_unicode_accepted(self):
        """Peer ID with unicode characters should be accepted."""
        config = PeerRegistryConfig(
            peer_id="registry-中文",
            name="Central Registry",
            endpoint="https://registry.example.com",
        )
        assert config.peer_id == "registry-中文"

    def test_name_unicode_accepted(self):
        """Name with unicode characters should be accepted."""
        config = PeerRegistryConfig(
            peer_id="central-registry",
            name="中央注册表",
            endpoint="https://registry.example.com",
        )
        assert config.name == "中央注册表"

    def test_json_serialization(self):
        """Config should serialize to JSON correctly."""
        config = PeerRegistryConfig(
            peer_id="central-registry",
            name="Central Registry",
            endpoint="https://registry.example.com",
        )
        json_str = config.model_dump_json()
        data = json.loads(json_str)
        assert data["peer_id"] == "central-registry"
        assert data["name"] == "Central Registry"
        assert data["endpoint"] == "https://registry.example.com"

    def test_json_deserialization(self):
        """Config should deserialize from JSON correctly."""
        json_data = {
            "peer_id": "central-registry",
            "name": "Central Registry",
            "endpoint": "https://registry.example.com",
        }
        config = PeerRegistryConfig(**json_data)
        assert config.peer_id == "central-registry"
        assert config.name == "Central Registry"

    def test_model_has_json_schema(self):
        """Model should generate JSON schema for OpenAPI."""
        schema = PeerRegistryConfig.model_json_schema()
        assert schema["type"] == "object"
        assert "properties" in schema
        assert "peer_id" in schema["properties"]
        assert "name" in schema["properties"]
        assert "endpoint" in schema["properties"]


# =============================================================================
# Test SyncMetadata Model
# =============================================================================


@pytest.mark.unit
class TestSyncMetadata:
    """Tests for SyncMetadata model."""

    def test_valid_minimal_metadata(self):
        """Minimal valid metadata should be accepted."""
        now = datetime.now(UTC)
        metadata = SyncMetadata(
            upstream_peer_id="central-registry",
            upstream_path="/finance-tools",
            last_synced_at=now,
        )
        assert metadata.upstream_peer_id == "central-registry"
        assert metadata.upstream_path == "/finance-tools"
        assert metadata.sync_generation == 1
        assert metadata.is_orphaned is False
        assert metadata.is_read_only is True

    def test_valid_full_metadata(self):
        """Full metadata with all fields should be accepted."""
        now = datetime.now(UTC)
        orphaned_time = now - timedelta(days=1)
        metadata = SyncMetadata(
            upstream_peer_id="central-registry",
            upstream_path="/finance-tools",
            sync_generation=42,
            last_synced_at=now,
            is_orphaned=True,
            orphaned_at=orphaned_time,
            local_overrides={"tags": ["local-tag"]},
            is_read_only=False,
        )
        assert metadata.sync_generation == 42
        assert metadata.is_orphaned is True
        assert metadata.orphaned_at == orphaned_time
        assert metadata.local_overrides == {"tags": ["local-tag"]}
        assert metadata.is_read_only is False

    def test_sync_generation_minimum_enforced(self):
        """Sync generation below 1 should raise validation error."""
        now = datetime.now(UTC)
        with pytest.raises(ValidationError):
            SyncMetadata(
                upstream_peer_id="central-registry",
                upstream_path="/finance-tools",
                sync_generation=0,
                last_synced_at=now,
            )

    def test_orphaned_at_auto_set(self):
        """orphaned_at should be auto-set when is_orphaned is True."""
        now = datetime.now(UTC)
        metadata = SyncMetadata(
            upstream_peer_id="central-registry",
            upstream_path="/finance-tools",
            last_synced_at=now,
            is_orphaned=True,
        )
        assert metadata.orphaned_at is not None
        assert isinstance(metadata.orphaned_at, datetime)

    def test_datetime_serialization(self):
        """Datetime fields should serialize correctly."""
        now = datetime.now(UTC)
        metadata = SyncMetadata(
            upstream_peer_id="central-registry",
            upstream_path="/finance-tools",
            last_synced_at=now,
        )
        json_str = metadata.model_dump_json()
        data = json.loads(json_str)
        assert "last_synced_at" in data
        assert isinstance(data["last_synced_at"], str)

    def test_datetime_deserialization(self):
        """Datetime fields should deserialize correctly."""
        now = datetime.now(UTC)
        json_data = {
            "upstream_peer_id": "central-registry",
            "upstream_path": "/finance-tools",
            "last_synced_at": now.isoformat(),
        }
        metadata = SyncMetadata(**json_data)
        assert isinstance(metadata.last_synced_at, datetime)

    def test_local_overrides_dict(self):
        """local_overrides should accept dictionary."""
        now = datetime.now(UTC)
        overrides = {"tags": ["tag1"], "description": "Custom desc"}
        metadata = SyncMetadata(
            upstream_peer_id="central-registry",
            upstream_path="/finance-tools",
            last_synced_at=now,
            local_overrides=overrides,
        )
        assert metadata.local_overrides == overrides

    def test_model_has_json_schema(self):
        """Model should generate JSON schema for OpenAPI."""
        schema = SyncMetadata.model_json_schema()
        assert schema["type"] == "object"
        assert "properties" in schema
        assert "upstream_peer_id" in schema["properties"]
        assert "last_synced_at" in schema["properties"]


# =============================================================================
# Test SyncHistoryEntry Model
# =============================================================================


@pytest.mark.unit
class TestSyncHistoryEntry:
    """Tests for SyncHistoryEntry model."""

    def test_valid_minimal_entry(self):
        """Minimal valid sync history entry should be accepted."""
        now = datetime.now(UTC)
        entry = SyncHistoryEntry(
            sync_id="sync-123",
            started_at=now,
        )
        assert entry.sync_id == "sync-123"
        assert entry.success is False
        assert entry.servers_synced == 0
        assert entry.agents_synced == 0

    def test_valid_successful_entry(self):
        """Successful sync entry with all fields should be accepted."""
        started = datetime.now(UTC)
        completed = started + timedelta(seconds=15)
        entry = SyncHistoryEntry(
            sync_id="sync-123",
            started_at=started,
            completed_at=completed,
            success=True,
            servers_synced=42,
            agents_synced=15,
            servers_orphaned=2,
            agents_orphaned=1,
            sync_generation=100,
            full_sync=False,
        )
        assert entry.success is True
        assert entry.servers_synced == 42
        assert entry.agents_synced == 15
        assert entry.servers_orphaned == 2
        assert entry.agents_orphaned == 1

    def test_valid_failed_entry(self):
        """Failed sync entry with error message should be accepted."""
        now = datetime.now(UTC)
        entry = SyncHistoryEntry(
            sync_id="sync-123",
            started_at=now,
            completed_at=now,
            success=False,
            error_message="Connection timeout",
        )
        assert entry.success is False
        assert entry.error_message == "Connection timeout"

    def test_negative_counts_rejected(self):
        """Negative sync counts should be rejected."""
        now = datetime.now(UTC)
        with pytest.raises(ValidationError):
            SyncHistoryEntry(
                sync_id="sync-123",
                started_at=now,
                servers_synced=-1,
            )

    def test_model_has_json_schema(self):
        """Model should generate JSON schema for OpenAPI."""
        schema = SyncHistoryEntry.model_json_schema()
        assert schema["type"] == "object"
        assert "properties" in schema


# =============================================================================
# Test PeerSyncStatus Model
# =============================================================================


@pytest.mark.unit
class TestPeerSyncStatus:
    """Tests for PeerSyncStatus model."""

    def test_valid_minimal_status(self):
        """Minimal valid sync status should be accepted."""
        status = PeerSyncStatus(
            peer_id="central-registry",
        )
        assert status.peer_id == "central-registry"
        assert status.is_healthy is False
        assert status.current_generation == 0
        assert status.sync_in_progress is False
        assert len(status.sync_history) == 0

    def test_valid_full_status(self):
        """Full sync status with all fields should be accepted."""
        now = datetime.now(UTC)
        status = PeerSyncStatus(
            peer_id="central-registry",
            is_healthy=True,
            last_health_check=now,
            last_successful_sync=now,
            last_sync_attempt=now,
            current_generation=100,
            total_servers_synced=42,
            total_agents_synced=15,
            sync_in_progress=True,
            consecutive_failures=0,
        )
        assert status.is_healthy is True
        assert status.current_generation == 100
        assert status.total_servers_synced == 42

    def test_add_history_entry(self):
        """Adding history entry should work correctly."""
        now = datetime.now(UTC)
        status = PeerSyncStatus(peer_id="central-registry")
        entry = SyncHistoryEntry(
            sync_id="sync-123",
            started_at=now,
        )
        status.add_history_entry(entry)
        assert len(status.sync_history) == 1
        assert status.sync_history[0] == entry

    def test_add_history_entry_maintains_max_limit(self):
        """Adding entries beyond max should maintain limit."""
        now = datetime.now(UTC)
        status = PeerSyncStatus(peer_id="central-registry")

        # Add more than max entries
        for i in range(MAX_SYNC_HISTORY_ENTRIES + 10):
            entry = SyncHistoryEntry(
                sync_id=f"sync-{i}",
                started_at=now,
            )
            status.add_history_entry(entry)

        assert len(status.sync_history) == MAX_SYNC_HISTORY_ENTRIES

    def test_add_history_entry_newest_first(self):
        """Newest history entries should appear first."""
        now = datetime.now(UTC)
        status = PeerSyncStatus(peer_id="central-registry")

        entry1 = SyncHistoryEntry(sync_id="sync-1", started_at=now)
        entry2 = SyncHistoryEntry(sync_id="sync-2", started_at=now)

        status.add_history_entry(entry1)
        status.add_history_entry(entry2)

        assert status.sync_history[0].sync_id == "sync-2"
        assert status.sync_history[1].sync_id == "sync-1"

    def test_model_has_json_schema(self):
        """Model should generate JSON schema for OpenAPI."""
        schema = PeerSyncStatus.model_json_schema()
        assert schema["type"] == "object"
        assert "properties" in schema


# =============================================================================
# Test SyncResult Model
# =============================================================================


@pytest.mark.unit
class TestSyncResult:
    """Tests for SyncResult model."""

    def test_valid_successful_result(self):
        """Valid successful sync result should be accepted."""
        result = SyncResult(
            success=True,
            peer_id="central-registry",
            servers_synced=42,
            agents_synced=15,
            duration_seconds=12.5,
            new_generation=101,
        )
        assert result.success is True
        assert result.servers_synced == 42
        assert result.duration_seconds == 12.5

    def test_valid_failed_result(self):
        """Valid failed sync result with error should be accepted."""
        result = SyncResult(
            success=False,
            peer_id="central-registry",
            error_message="Connection timeout",
            duration_seconds=5.0,
        )
        assert result.success is False
        assert result.error_message == "Connection timeout"

    def test_negative_duration_rejected(self):
        """Negative duration should be rejected."""
        with pytest.raises(ValidationError):
            SyncResult(
                success=True,
                peer_id="central-registry",
                duration_seconds=-1.0,
            )

    def test_model_has_json_schema(self):
        """Model should generate JSON schema for OpenAPI."""
        schema = SyncResult.model_json_schema()
        assert schema["type"] == "object"
        assert "properties" in schema


# =============================================================================
# Test FederationExportResponse Model
# =============================================================================


@pytest.mark.unit
class TestFederationExportResponse:
    """Tests for FederationExportResponse model."""

    def test_valid_minimal_response(self):
        """Minimal valid export response should be accepted."""
        response = FederationExportResponse(
            sync_generation=100,
            total_count=0,
            registry_id="central-registry",
        )
        assert response.sync_generation == 100
        assert response.total_count == 0
        assert response.has_more is False
        assert len(response.items) == 0

    def test_valid_full_response(self):
        """Full export response with items should be accepted."""
        items = [
            {"path": "/server1", "name": "Server 1"},
            {"path": "/server2", "name": "Server 2"},
        ]
        response = FederationExportResponse(
            items=items,
            sync_generation=100,
            total_count=10,
            has_more=True,
            registry_id="central-registry",
        )
        assert len(response.items) == 2
        assert response.has_more is True
        assert response.total_count == 10

    def test_empty_items_list(self):
        """Empty items list should be accepted."""
        response = FederationExportResponse(
            items=[],
            sync_generation=100,
            total_count=0,
            registry_id="central-registry",
        )
        assert response.items == []

    def test_negative_total_count_rejected(self):
        """Negative total_count should be rejected."""
        with pytest.raises(ValidationError):
            FederationExportResponse(
                sync_generation=100,
                total_count=-1,
                registry_id="central-registry",
            )

    def test_model_has_json_schema(self):
        """Model should generate JSON schema for OpenAPI."""
        schema = FederationExportResponse.model_json_schema()
        assert schema["type"] == "object"
        assert "properties" in schema
        assert "items" in schema["properties"]


# =============================================================================
# Test Backward Compatibility
# =============================================================================


@pytest.mark.unit
class TestBackwardCompatibility:
    """Tests to ensure backward compatibility with existing schemas."""

    def test_server_detail_still_works(self):
        """Verify that server models still serialize correctly."""
        # This is a basic smoke test - actual server models tested elsewhere
        from registry.schemas.anthropic_schema import ServerDetail

        server = ServerDetail(
            name="Test Server",
            description="Test description",
            version="1.0.0",
            repository={"url": "https://github.com/test/repo", "source": "github"},
        )

        # Should serialize without errors
        json_str = server.model_dump_json()
        assert json_str is not None

        # Should deserialize without errors
        data = json.loads(json_str)
        server2 = ServerDetail(**data)
        assert server2.name == "Test Server"

    def test_agent_card_still_works(self):
        """Verify that agent models still serialize correctly."""
        from registry.schemas.agent_models import AgentCard, Skill

        agent = AgentCard(
            version="1.0.0",
            protocol_version="1.0",
            name="Test Agent",
            description="Test description",
            url="https://example.com",
            path="/test-agent",
            visibility="internal",
            trust_level="verified",
            skills=[
                Skill(
                    id="test",
                    name="Test Skill",
                    description="Test",
                    tags=["test"],
                )
            ],
        )

        # Should serialize without errors
        json_str = agent.model_dump_json()
        assert json_str is not None

        # Should deserialize without errors
        data = json.loads(json_str)
        agent2 = AgentCard(**data)
        assert agent2.name == "Test Agent"


# =============================================================================
# Test Edge Cases and Special Scenarios
# =============================================================================


@pytest.mark.unit
class TestEdgeCases:
    """Tests for edge cases and special scenarios."""

    def test_peer_config_with_all_sync_modes(self):
        """Test creating configs with each sync mode."""
        modes = ["all", "whitelist", "tag_filter"]
        for mode in modes:
            config = PeerRegistryConfig(
                peer_id=f"peer-{mode}",
                name=f"Peer {mode}",
                endpoint="https://registry.example.com",
                sync_mode=mode,
            )
            assert config.sync_mode == mode

    def test_unicode_in_all_string_fields(self):
        """Test unicode support in all string fields."""
        config = PeerRegistryConfig(
            peer_id="registry-中文-日本語",
            name="مسجل / реестр / レジストリ",
            endpoint="https://registry.example.com",
        )
        assert "中文" in config.peer_id
        assert "مسجل" in config.name

    def test_very_long_field_values(self):
        """Test handling of very long field values."""
        # Name has max_length=255, so test at the boundary
        long_name = "A" * 255
        config = PeerRegistryConfig(
            peer_id="test",
            name=long_name,
            endpoint="https://registry.example.com",
        )
        assert len(config.name) == 255

        # Test that exceeding max_length fails
        with pytest.raises(ValidationError):
            PeerRegistryConfig(
                peer_id="test",
                name="A" * 256,
                endpoint="https://registry.example.com",
            )

    def test_special_characters_in_allowed_fields(self):
        """Test special characters in fields where they're allowed."""
        config = PeerRegistryConfig(
            peer_id="test-peer_123",
            name="Test: Peer (Production) [v2.0]",
            endpoint="https://registry.example.com:8080/api/v2",
        )
        assert ":" in config.name
        assert "(" in config.name
        assert ":8080" in config.endpoint

    def test_datetime_with_timezone(self):
        """Test datetime fields with various timezones."""
        utc_time = datetime.now(UTC)
        metadata = SyncMetadata(
            upstream_peer_id="test",
            upstream_path="/test",
            last_synced_at=utc_time,
        )
        assert metadata.last_synced_at.tzinfo is not None

    def test_empty_local_overrides(self):
        """Test SyncMetadata with empty local_overrides."""
        now = datetime.now(UTC)
        metadata = SyncMetadata(
            upstream_peer_id="test",
            upstream_path="/test",
            last_synced_at=now,
            local_overrides={},
        )
        assert metadata.local_overrides == {}

    def test_zero_values_in_numeric_fields(self):
        """Test zero values in numeric fields where allowed."""
        now = datetime.now(UTC)
        entry = SyncHistoryEntry(
            sync_id="sync-0",
            started_at=now,
            servers_synced=0,
            agents_synced=0,
            servers_orphaned=0,
            agents_orphaned=0,
            sync_generation=0,
        )
        assert entry.servers_synced == 0
        assert entry.sync_generation == 0

    def test_model_round_trip_serialization(self):
        """Test complete serialization round-trip for all models."""
        now = datetime.now(UTC)

        # Test each model
        models = [
            PeerRegistryConfig(
                peer_id="test",
                name="Test",
                endpoint="https://example.com",
            ),
            SyncMetadata(
                upstream_peer_id="test",
                upstream_path="/test",
                last_synced_at=now,
            ),
            SyncHistoryEntry(
                sync_id="sync-1",
                started_at=now,
            ),
            PeerSyncStatus(peer_id="test"),
            SyncResult(success=True, peer_id="test"),
            FederationExportResponse(
                sync_generation=1,
                total_count=0,
                registry_id="test",
            ),
        ]

        for model in models:
            # Serialize to JSON
            json_str = model.model_dump_json()
            # Deserialize back
            data = json.loads(json_str)
            model2 = type(model)(**data)
            # Should be equivalent
            assert model.model_dump() == model2.model_dump()
