"""Path-bound report token lifecycle (add-self-reported-tool-surface 1.2)."""

from __future__ import annotations

from registry.api import surface_report_routes as mod


def test_issue_validate_revoke():
    entry = {"server_name": "wire-build-mcp", "surface_source": "probe"}

    token, updated = mod.issue_surface_token(entry)

    # The plaintext is returned once and never stored.
    assert token.startswith("srf_")
    assert updated[mod.SURFACE_TOKEN_FIELD] != token
    assert token not in updated.values()
    assert updated["surface_source"] == "self_report"
    # The input card is not mutated (callers hold repository state).
    assert mod.SURFACE_TOKEN_FIELD not in entry

    assert mod._token_matches(updated, token) is True
    assert mod._token_matches(updated, token + "x") is False
    assert mod._token_matches({"server_name": "x"}, token) is False

    revoked = mod.revoke_surface_token(updated)
    assert mod.SURFACE_TOKEN_FIELD not in revoked
    assert revoked["surface_source"] == "probe"
    assert mod._token_matches(revoked, token) is False


def test_tokens_are_unique_per_issue():
    a, _ = mod.issue_surface_token({})
    b, _ = mod.issue_surface_token({})
    assert a != b


def test_hash_is_sha256_not_reversible():
    import hashlib

    token, updated = mod.issue_surface_token({})
    assert updated[mod.SURFACE_TOKEN_FIELD] == hashlib.sha256(token.encode()).hexdigest()
