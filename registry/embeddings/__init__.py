"""Embeddings module for vendor-agnostic embeddings generation."""

from .client import (
    EmbeddingsClient,
    FastembedClient,
    LiteLLMClient,
    SentenceTransformersClient,
    create_embeddings_client,
)
from .token_provider import (
    EmbeddingsTokenProvider,
)

__all__ = [
    "EmbeddingsClient",
    "FastembedClient",
    "SentenceTransformersClient",
    "LiteLLMClient",
    "EmbeddingsTokenProvider",
    "create_embeddings_client",
]
