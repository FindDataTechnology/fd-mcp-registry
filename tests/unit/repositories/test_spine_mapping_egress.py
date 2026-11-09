"""Round-trip: per-user egress config survives the spine doc <-> payload mapping.

``POST /servers/<path>/egress-auth`` assembles ``egress_auth_mode`` /
``egress_oauth`` on the legacy document and persists it through
:func:`mcp_doc_to_upsert_entry`; every read promotes it back with
:func:`mcp_row_to_doc`.

Before these two keys were promoted, the mapping demoted nothing and promoted
nothing, so in replica mode the route answered **200 while writing nothing**:
the response echoed the in-memory dict the handler had just built, the replica
kept ``egress_auth_mode=NULL``, and every following ``PUT /egress-pat`` failed
closed with 409 "server is not configured for pat". Net effect: per-user
servers (pat / oauth_user / obo_exchange) were structurally unreachable through
the gateway in replica mode — 2026-11-09 production evidence on /wire-build-mcp.
"""

import pytest

from registry.repositories.spine.mapping import (
    MCP_METADATA_PROMOTIONS,
    mcp_doc_fields_to_patch_fields,
    mcp_doc_to_upsert_entry,
    mcp_row_to_doc,
)

_EGRESS_DOC = {
    "path": "/wire-build-mcp",
    "server_name": "wire-build-mcp",
    "description": "wire artifact builder",
    "proxy_pass_url": "http://cheap6.mesh.finddata.cn:31898/mcp",
    "num_tools": 17,
    "egress_auth_mode": "pat",
    "egress_oauth": {"provider": "wire-customer", "scopes": []},
    "is_enabled": True,
}


@pytest.mark.unit
class TestEgressFieldsSurviveTheMapping:
    def test_promotions_carry_both_egress_keys(self):
        assert "egress_auth_mode" in MCP_METADATA_PROMOTIONS
        assert "egress_oauth" in MCP_METADATA_PROMOTIONS

    def test_write_demotes_egress_into_metadata(self):
        entry = mcp_doc_to_upsert_entry(dict(_EGRESS_DOC))
        assert entry["metadata"]["egress_auth_mode"] == "pat"
        assert entry["metadata"]["egress_oauth"] == {"provider": "wire-customer", "scopes": []}

    def test_read_promotes_egress_back_to_the_document(self):
        # The shape the replica stores: one payload row per server.
        row = {
            "id": "/wire-build-mcp",
            "display_name": "wire-build-mcp",
            "description": "wire artifact builder",
            "proxy_pass_url": "http://cheap6.mesh.finddata.cn:31898/mcp",
            "num_tools": 17,
            "is_enabled": 1,
            "metadata": {
                "egress_auth_mode": "pat",
                "egress_oauth": {"provider": "wire-customer", "scopes": []},
            },
        }
        doc = mcp_row_to_doc(row)
        assert doc["egress_auth_mode"] == "pat"
        assert doc["egress_oauth"]["provider"] == "wire-customer"

    def test_full_round_trip_is_lossless(self):
        entry = mcp_doc_to_upsert_entry(dict(_EGRESS_DOC))
        row = {
            "id": entry["id"],
            "display_name": entry.get("display_name"),
            "description": entry.get("description"),
            "proxy_pass_url": entry.get("proxy_pass_url"),
            "num_tools": entry.get("num_tools"),
            "is_enabled": entry.get("is_enabled"),
            "metadata": entry["metadata"],
        }
        doc = mcp_row_to_doc(row)
        assert doc["egress_auth_mode"] == _EGRESS_DOC["egress_auth_mode"]
        assert doc["egress_oauth"] == _EGRESS_DOC["egress_oauth"]

    def test_patch_path_forwards_egress_fields_instead_of_reporting_them_unmapped(self):
        fields, unmapped = mcp_doc_fields_to_patch_fields(
            dict(_EGRESS_DOC), ["egress_auth_mode", "egress_oauth"]
        )
        assert "egress_auth_mode" not in unmapped
        assert "egress_oauth" not in unmapped
        assert fields["metadata"]["egress_auth_mode"] == "pat"
        assert fields["metadata"]["egress_oauth"]["provider"] == "wire-customer"
