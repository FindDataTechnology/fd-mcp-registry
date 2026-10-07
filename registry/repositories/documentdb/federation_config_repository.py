"""DocumentDB repository for federation configuration storage."""

import logging
from datetime import UTC, datetime
from typing import Any

from motor.motor_asyncio import AsyncIOMotorCollection

from ...schemas.federation_schema import FederationConfig
from ..interfaces import FederationConfigRepositoryBase
from .client import get_collection_name, get_documentdb_client

logger = logging.getLogger(__name__)


class DocumentDBFederationConfigRepository(FederationConfigRepositoryBase):
    """DocumentDB implementation of federation configuration repository."""

    def __init__(self):
        self._collection: AsyncIOMotorCollection | None = None
        self._collection_name = get_collection_name("mcp_federation_config")
        logger.info(
            f"Initialized DocumentDB FederationConfigRepository with collection: "
            f"{self._collection_name}"
        )

    async def _get_collection(self) -> AsyncIOMotorCollection:
        """Get DocumentDB collection."""
        if self._collection is None:
            db = await get_documentdb_client()
            self._collection = db[self._collection_name]
        return self._collection

    async def get_config(self, config_id: str = "default") -> FederationConfig | None:
        """Get federation configuration by ID."""
        try:
            collection = await self._get_collection()

            config_doc = await collection.find_one({"_id": config_id})

            if not config_doc:
                logger.info(f"Federation config not found: {config_id}")
                return None

            config_doc.pop("_id", None)
            config_doc.pop("created_at", None)
            config_doc.pop("updated_at", None)

            config = FederationConfig(**config_doc)
            logger.info(f"Retrieved federation config: {config_id}")
            return config

        except Exception as e:
            logger.error(f"Failed to get federation config {config_id}: {e}", exc_info=True)
            return None

    async def save_config(
        self, config: FederationConfig, config_id: str = "default"
    ) -> FederationConfig:
        """Save or update federation configuration."""
        try:
            collection = await self._get_collection()

            existing = await collection.find_one({"_id": config_id})

            doc = config.model_dump()

            now = datetime.now(UTC).isoformat()
            if existing:
                doc["created_at"] = existing.get("created_at", now)
                doc["updated_at"] = now
            else:
                doc["created_at"] = now
                doc["updated_at"] = now

            doc["_id"] = config_id

            await collection.replace_one({"_id": config_id}, doc, upsert=True)

            logger.info(f"Saved federation config: {config_id}")
            return config

        except Exception as e:
            logger.error(f"Failed to save federation config {config_id}: {e}", exc_info=True)
            raise

    async def delete_config(self, config_id: str = "default") -> bool:
        """Delete federation configuration."""
        try:
            collection = await self._get_collection()

            result = await collection.delete_one({"_id": config_id})

            if result.deleted_count == 0:
                logger.warning(f"Federation config not found for deletion: {config_id}")
                return False

            logger.info(f"Deleted federation config: {config_id}")
            return True

        except Exception as e:
            logger.error(f"Failed to delete federation config {config_id}: {e}", exc_info=True)
            return False

    async def list_configs(self) -> list[dict[str, Any]]:
        """List all federation configurations."""
        try:
            collection = await self._get_collection()

            cursor = collection.find({}, {"_id": 1, "created_at": 1, "updated_at": 1})

            configs = []
            async for doc in cursor:
                configs.append(
                    {
                        "id": doc.get("_id"),
                        "created_at": doc.get("created_at"),
                        "updated_at": doc.get("updated_at"),
                    }
                )

            logger.info(f"Listed {len(configs)} federation configs")
            return configs

        except Exception as e:
            logger.error(f"Failed to list federation configs: {e}", exc_info=True)
            return []
