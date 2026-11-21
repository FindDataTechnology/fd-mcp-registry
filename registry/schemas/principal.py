"""Principal (user / agent / service) and tenant registration schemas.

A principal is one caller identity. Three types share one namespace:

- ``user``    — a human, identified by their IdP subject.
- ``agent``   — an AI principal that acts on behalf of a human; it MUST have an
                ``owner_principal_id`` pointing at an existing principal, so
                usage and authority trace back to a human.
- ``service`` — a machine identity (CI, a cron, a peer gateway).

A tenant is the unit a principal belongs to. It carries the two configs that
are shared across all its principals — billing binding and entitlements — so
those facts never live in two places. It also declares an ``isolation_tier``:
``shared`` (one instance, storage-layer partitioning), ``dedicated_cell`` (a
process + data root per tenant, the wire cell shape) or ``dedicated_fleet`` (a
whole agent fleet). The tier decides WHERE a request lands; it never makes the
engine multi-tenant-aware — only ``shared`` storage partitioning does.

Change: establish-agent-principal-plane (task group 1). Storage is MongoDB,
same client as the rest of the registry; collections ``principals`` /
``tenants``.
"""

from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


class PrincipalType(str, Enum):
    """Caller kinds sharing one principal namespace."""

    USER = "user"
    AGENT = "agent"
    SERVICE = "service"


class PrincipalStatus(str, Enum):
    """Lifecycle state. ``revoked`` is terminal."""

    ACTIVE = "active"
    SUSPENDED = "suspended"
    REVOKED = "revoked"


class IsolationTier(str, Enum):
    """Where a tenant's requests land. Missing tier => refuse (no default)."""

    SHARED = "shared"
    DEDICATED_CELL = "dedicated_cell"
    DEDICATED_FLEET = "dedicated_fleet"


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Tenant(BaseModel):
    """A tenant: the shared config boundary for a set of principals."""

    id: str = Field(..., min_length=1, max_length=128)
    name: str = Field(default="", max_length=256)
    isolation_tier: IsolationTier | None = Field(
        default=None,
        description=(
            "Where this tenant's requests land. None means unconfigured; grant "
            "issuance for a principal in such a tenant is refused with "
            "tenant_tier_unresolved. There is NO default tier."
        ),
    )
    billing_binding: str | None = Field(
        default=None,
        description="Which settlement account this tenant's usage is booked to.",
    )
    entitlements: dict[str, Any] | list[Any] | None = Field(
        default=None,
        description=(
            "Authorization set projected onto the tenant (principal scope, tool "
            "scope). Principals inherit it; they MUST NOT carry a duplicate copy."
        ),
    )
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class TenantUpsert(BaseModel):
    """Request body for creating/updating a tenant."""

    id: str = Field(..., min_length=1, max_length=128)
    name: str = Field(default="", max_length=256)
    isolation_tier: IsolationTier | None = None
    billing_binding: str | None = None
    entitlements: dict[str, Any] | list[Any] | None = None


class Principal(BaseModel):
    """One caller identity."""

    id: str = Field(..., min_length=1, max_length=512)
    type: PrincipalType
    tenant_id: str = Field(..., min_length=1, max_length=128)
    owner_principal_id: str | None = Field(
        default=None,
        description="Required for type=agent; the principal this agent acts for.",
    )
    status: PrincipalStatus = PrincipalStatus.ACTIVE
    display_name: str = Field(default="", max_length=256)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    def model_post_init(self, __context: object) -> None:  # noqa: D401
        if self.type is PrincipalType.AGENT and not self.owner_principal_id:
            raise ValueError("agent principal requires owner_principal_id")


class PrincipalCreate(BaseModel):
    """Request body for registering a principal."""

    id: str = Field(..., min_length=1, max_length=512)
    type: PrincipalType
    tenant_id: str = Field(..., min_length=1, max_length=128)
    owner_principal_id: str | None = None
    display_name: str = Field(default="", max_length=256)

    @field_validator("owner_principal_id")
    @classmethod
    def _agent_needs_owner(cls, v: str | None, info) -> str | None:  # type: ignore[no-untyped-def]
        if info.data.get("type") is PrincipalType.AGENT and not v:
            raise ValueError("agent principal requires owner_principal_id")
        return v

    @model_validator(mode="after")
    def _agent_owner_required(self) -> "PrincipalCreate":
        # Field validators skip defaults, so an omitted owner on an agent would
        # slip through; enforce on the built model.
        if self.type is PrincipalType.AGENT and not self.owner_principal_id:
            raise ValueError("agent principal requires owner_principal_id")
        return self


class PrincipalStatusUpdate(BaseModel):
    """Request body for a status transition."""

    status: PrincipalStatus
