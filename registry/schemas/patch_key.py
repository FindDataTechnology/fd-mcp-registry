"""Per-user long-lived API key ("patch key") schemas for MongoDB storage.

A patch key is a non-expiring Bearer credential that a logged-in (console
session) user mints for machine clients. The plaintext key is shown exactly
once in the mint response; the database stores only a SHA-256 hash plus
metadata. The auth server's /validate resolves an ``wgk-`` prefixed Bearer
token by hash and continues the request as the owning user (same
groups/scope chain the user gets with a JWT).

The stored snapshot of the owner's groups is taken at mint time: revoking the
owner's session does not revoke the key (use the revoke endpoint), but scope
mappings are resolved per request so scope changes take effect immediately.
"""

from datetime import datetime

from pydantic import BaseModel, Field, field_validator, model_validator

# Key lifecycle states. Revocation is one-way: there is no transition back to
# active (the API exposes no un-revoke).
PATCH_KEY_STATUS_ACTIVE: str = "active"
PATCH_KEY_STATUS_REVOKED: str = "revoked"

_KEY_NAME_PATTERN_MAX: int = 128


class PatchKeyCreate(BaseModel):
    """Request body for POST /api/patch-keys.

    ``username``/``groups`` are the admin-only mint-on-behalf-of fields
    (add-customer-onboarding-automation 4.1): when EITHER is present the
    route requires an admin caller and the minted key is owned by ``username``
    with exactly the given groups snapshot — never the caller's own. They are
    designed as a pair: minting with custom groups but the caller's username
    (or vice versa) is the mis-shaped half that produces keys with the wrong
    owner or an accidental privilege copy, so the fields are validated to be
    both-or-neither.
    """

    name: str = Field(
        ...,
        min_length=1,
        max_length=_KEY_NAME_PATTERN_MAX,
        description="Human-readable label for the key (e.g. 'ci-runner')",
    )
    username: str | None = Field(
        default=None,
        min_length=1,
        max_length=256,
        description=(
            "ADMIN ONLY: mint on behalf of this user (the key's owner/sub). "
            "Must be set together with groups."
        ),
    )
    groups: list[str] | None = Field(
        default=None,
        min_length=1,
        description=(
            "ADMIN ONLY: exact groups snapshot for the minted key (e.g. "
            '["wire-customers", "wire-cust-<cid>"]). Must be set together '
            "with username."
        ),
    )

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("name must not be blank")
        return v

    @field_validator("username")
    @classmethod
    def _strip_username(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            raise ValueError("username must not be blank")
        return v

    @field_validator("groups")
    @classmethod
    def _clean_groups(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        cleaned = sorted({g.strip() for g in v if g.strip()})
        if not cleaned:
            raise ValueError("groups must contain at least one non-blank name")
        return cleaned

    @model_validator(mode="after")
    def _pair_username_groups(self) -> "PatchKeyCreate":
        if (self.username is None) != (self.groups is None):
            raise ValueError(
                "username and groups are admin mint-on-behalf-of fields and "
                "must be provided together (owner + exact snapshot, as a pair)"
            )
        return self


class PatchKeyInfo(BaseModel):
    """Metadata view of a patch key. Never contains the plaintext or its hash."""

    key_id: str = Field(..., description="Server-generated opaque key identifier")
    name: str = Field(..., description="Human-readable label chosen at mint time")
    key_prefix: str = Field(
        ...,
        description=(
            "First characters of the plaintext key (e.g. 'wgk-ab12cd34'), safe "
            "for display/correlation; not sufficient to reconstruct the key"
        ),
    )
    username: str = Field(..., description="Owning user (login username / OIDC sub)")
    email: str | None = Field(None, description="Owning user's email captured at mint time")
    provider: str | None = Field(
        None,
        description=(
            "Auth method / IdP the owner's console session used at mint time "
            "(audit labeling only; not used for authorization)"
        ),
    )
    egress_user: str | None = Field(
        None,
        description=(
            "Canonical per-user egress vault id (OIDC sub) captured at mint "
            "time; keys the wgk- acceptance path's egress vend to the owner's "
            "vault bucket. None for legacy keys minted before this field."
        ),
    )
    groups: list[str] = Field(
        default_factory=list,
        description="Snapshot of the owner's groups taken at mint time",
    )
    status: str = Field(
        ..., description=f"'{PATCH_KEY_STATUS_ACTIVE}' or '{PATCH_KEY_STATUS_REVOKED}'"
    )
    created_at: datetime = Field(..., description="When the key was minted")
    last_used_at: datetime | None = Field(
        None, description="When the key last authenticated a request (None if never used)"
    )
    revoked_at: datetime | None = Field(
        None, description="When the key was revoked (None while active)"
    )


class PatchKeyCreated(BaseModel):
    """Response for POST /api/patch-keys.

    ``key`` is the plaintext Bearer value. It is returned exactly once and is
    not recoverable afterwards; the database stores only its SHA-256 hash.
    """

    key: str = Field(..., description="Plaintext API key (shown once, store it now)")
    info: PatchKeyInfo = Field(..., description="Persisted key metadata")


class PatchKeyGrantView(BaseModel):
    """Owner's monthly call-grant usage (ecosystem-bridge 3.3 visibility).

    One pool per OWNER across all their keys; None when the grant gate is
    disabled on this deployment (nothing is counted).
    """

    month: str = Field(..., description="Grant window key (YYYY-MM)")
    used: int = Field(..., description="Admitted tools/call count this window")
    limit: int = Field(..., description="Monthly free-grant limit")


class PatchKeyListResponse(BaseModel):
    """Response envelope for GET /api/patch-keys (caller's keys only)."""

    total: int = Field(..., description="Total number of the caller's keys")
    items: list[PatchKeyInfo] = Field(default_factory=list)
    grant: PatchKeyGrantView | None = Field(
        default=None,
        description="Monthly call-grant usage for the key owner, when enabled",
    )
