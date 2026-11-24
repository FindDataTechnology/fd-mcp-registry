"""Tool-face manifest schema (capability ``tool-face``).

A **tool face** is a first-class, declarative aggregation of MCP tools drawn
from one or more backend servers, served at a single stable path. This module
defines the manifest shape and its *structural* validation only:

  * cross-server tool selection with a stable ``alias`` per tool (aliases must
    be unique inside a face),
  * a brand policy applied at the face (see the ``brand_policy`` field), and
  * the face's audience / exposure declaration used by governance.

Closure against the *real* backend tool surface is deliberately NOT enforced
here: that requires the registry's server index and belongs to the importer
(``registry/services/face_manifest_service.py``), so this schema stays a pure,
cheap-to-test data contract.

Design note (D2/D3 in the change): the manifest is the single source of truth;
it is imported into registry ``virtual_server`` records, not hand-edited there.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: Keys a brand policy strips from a served payload when ``strip_provenance`` is
#: set. These are the upstream-attribution keys, and they are NOT the same as an
#: entity's legitimately-visible provenance (e.g. ``anchors[].source`` on an
#: entity row), which is why stripping is keyed on this explicit set rather than
#: "anything named source".
DEFAULT_PROVENANCE_KEYS: tuple[str, ...] = (
    "source_used",
    "real_source_used",
    "real_source",
    "source",
)

_ALIAS_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


class ToolSelection(BaseModel):
    """One ``(server, tool) -> alias`` mapping inside a face."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    backend: str = Field(
        ...,
        min_length=1,
        description="Backend server path as registered in the gateway (e.g. '/wire-ops-mcp').",
    )
    tool_name: str = Field(
        ...,
        min_length=1,
        description="Tool leaf name as exposed by the backend server.",
    )
    alias: str = Field(
        ...,
        min_length=1,
        description="Stable name this tool is served under inside the face. Required so the "
        "face name does not silently follow a backend rename.",
    )

    @field_validator("alias")
    @classmethod
    def _alias_charset(cls, v: str) -> str:
        if not _ALIAS_RE.match(v):
            raise ValueError("alias may contain only letters, digits, '_', '.', '-'")
        return v


class BrandPolicy(BaseModel):
    """How a face brands the payloads of its tools.

    ``strip_provenance=True`` removes upstream-attribution keys from responses so
    a commercial face exposes ``brand`` only. ``internal`` faces leave this False
    to retain source attribution.
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    strip_provenance: bool = Field(
        default=False,
        description="Strip upstream-attribution keys from served payloads.",
    )
    brand: str = Field(
        default="finddata",
        min_length=1,
        description="Brand value stamped on served values when stripping is on.",
    )
    provenance_keys: list[str] = Field(
        default_factory=lambda: list(DEFAULT_PROVENANCE_KEYS),
        description="Exact keys treated as upstream attribution and removed.",
    )


class FaceManifest(BaseModel):
    """Top-level manifest document."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    manifest_version: Literal[1] = 1
    face: str = Field(
        ...,
        min_length=1,
        description="Face identifier; the gateway path is '/virtual/<face>'.",
    )
    server_name: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="Human-readable name for the face.",
    )
    description: str = Field(default="", max_length=2048)
    audience: str = Field(
        ...,
        min_length=1,
        description="Who this face serves (governance/exposure label; not a runtime authz input).",
    )
    exposure: Literal["commercial", "internal"]
    brand_policy: BrandPolicy = Field(default_factory=BrandPolicy)
    tools: list[ToolSelection] = Field(..., min_length=1)
    required_scopes: list[str] = Field(default_factory=list)
    is_enabled: bool = Field(
        default=False,
        description="Imported faces start disabled unless explicitly enabled.",
    )

    @field_validator("face")
    @classmethod
    def _face_slug(cls, v: str) -> str:
        if not re.match(r"^[a-z0-9][a-z0-9-]*$", v):
            raise ValueError(
                "face must be a lowercase slug of [a-z0-9-] starting alphanumeric "
                "(the virtual path is '/virtual/<face>')"
            )
        return v

    @model_validator(mode="after")
    def _unique_aliases(self) -> FaceManifest:
        seen: dict[str, ToolSelection] = {}
        dupes: list[str] = []
        for sel in self.tools:
            if sel.alias in seen:
                dupes.append(
                    f"{sel.alias!r} (from {seen[sel.alias].backend}.{seen[sel.alias].tool_name} "
                    f"and {sel.backend}.{sel.tool_name})"
                )
            else:
                seen[sel.alias] = sel
        if dupes:
            raise ValueError("duplicate alias in face: " + ", ".join(dupes))
        return self

    @property
    def path(self) -> str:
        """Gateway path this face is served at."""
        return f"/virtual/{self.face}"


def load_face_manifest(path: str | Path) -> FaceManifest:
    """Load and validate one face manifest from a YAML file.

    Args:
        path: Path to a ``*.yaml`` face manifest.

    Returns:
        The validated :class:`FaceManifest`.

    Raises:
        FileNotFoundError: The path does not exist.
        ValueError: The file is not valid YAML, is not a mapping, or fails schema
            validation (the Pydantic error is chained).
    """
    p = Path(path)
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"face manifest {p} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"face manifest {p} must be a YAML mapping, got {type(raw).__name__}")
    try:
        return FaceManifest(**raw)
    except Exception as exc:  # pydantic ValidationError -> ValueError for callers
        raise ValueError(f"face manifest {p} failed schema validation: {exc}") from exc
