"""DocumentDB repository implementations using Motor (async MongoDB driver)."""

from .agent_repository import DocumentDBAgentRepository
from .client import (
    close_documentdb_client,
    get_collection_name,
    get_documentdb_client,
)
from .federation_config_repository import DocumentDBFederationConfigRepository
from .scope_repository import DocumentDBScopeRepository
from .search_repository import DocumentDBSearchRepository
from .security_scan_repository import DocumentDBSecurityScanRepository
from .server_repository import DocumentDBServerRepository

__all__ = [
    "DocumentDBAgentRepository",
    "DocumentDBFederationConfigRepository",
    "DocumentDBScopeRepository",
    "DocumentDBSearchRepository",
    "DocumentDBSecurityScanRepository",
    "DocumentDBServerRepository",
    "close_documentdb_client",
    "get_collection_name",
    "get_documentdb_client",
]
