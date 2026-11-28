"""Unit tests for the tool-face manifest schema (capability ``tool-face``)."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from registry.schemas.face_manifest import (
    DEFAULT_PROVENANCE_KEYS,
    BrandPolicy,
    FaceManifest,
    ToolSelection,
    load_face_manifest,
)

_FIXTURE = Path(__file__).parent.parent / "fixtures" / "faces" / "test-face.yaml"


def _manifest(**overrides) -> dict:
    base = {
        "face": "wire-customer",
        "server_name": "Wire Customer",
        "audience": "paying-customer",
        "exposure": "commercial",
        "tools": [
            {
                "backend": "/wire-ops-mcp",
                "tool_name": "wire_demand_submit",
                "alias": "demand_submit",
            },
            {
                "backend": "/wire-build-mcp",
                "tool_name": "wire_dataset_create",
                "alias": "dataset_create",
            },
        ],
    }
    base.update(overrides)
    return base


def test_path_derives_from_face():
    m = FaceManifest(**_manifest())
    assert m.path == "/virtual/wire-customer"


def test_cross_server_tools_accepted():
    m = FaceManifest(**_manifest())
    backends = {t.backend for t in m.tools}
    assert backends == {"/wire-ops-mcp", "/wire-build-mcp"}


def test_duplicate_alias_rejected_and_named():
    tools = [
        {"backend": "/open-data", "tool_name": "read", "alias": "read"},
        {"backend": "/business", "tool_name": "read", "alias": "read"},
    ]
    with pytest.raises(ValidationError) as exc:
        FaceManifest(**_manifest(tools=tools))
    assert "duplicate alias" in str(exc.value)
    assert "'read'" in str(exc.value)


def test_alias_unique_across_backends_ok():
    tools = [
        {"backend": "/open-data", "tool_name": "read", "alias": "market_read"},
        {"backend": "/business", "tool_name": "read", "alias": "customer_read"},
    ]
    m = FaceManifest(**_manifest(tools=tools))
    assert {t.alias for t in m.tools} == {"market_read", "customer_read"}


def test_face_slug_rejects_uppercase_and_space():
    with pytest.raises(ValidationError):
        FaceManifest(**_manifest(face="Wire Customer"))


def test_empty_tools_rejected():
    with pytest.raises(ValidationError):
        FaceManifest(**_manifest(tools=[]))


def test_alias_charset_rejects_space():
    with pytest.raises(ValidationError):
        ToolSelection(backend="/x", tool_name="y", alias="has space")


def test_brand_policy_defaults_strip_off():
    m = FaceManifest(**_manifest())
    assert m.brand_policy.strip_provenance is False
    assert m.brand_policy.brand == "finddata"


def test_brand_policy_provenance_keys_default_set():
    bp = BrandPolicy()
    assert set(bp.provenance_keys) == set(DEFAULT_PROVENANCE_KEYS)
    # The taxonomy must name the attribution keys and must NOT invent entity provenance.
    assert "source_used" in bp.provenance_keys
    assert "real_source_used" in bp.provenance_keys


def test_is_enabled_defaults_false():
    m = FaceManifest(**_manifest())
    assert m.is_enabled is False


def test_extra_field_forbidden():
    with pytest.raises(ValidationError):
        FaceManifest(**_manifest(unexpected_field="x"))


# --- loader ---


def test_load_fixture_manifest():
    m = load_face_manifest(_FIXTURE)
    assert m.face == "test-face"
    assert m.path == "/virtual/test-face"
    assert len(m.tools) == 2


def test_load_missing_file_raises():
    with pytest.raises(FileNotFoundError):
        load_face_manifest("/nonexistent/face.yaml")


def test_load_non_mapping_raises(tmp_path):
    p = tmp_path / "list.yaml"
    p.write_text("- just\n- a list\n", encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        load_face_manifest(p)
    assert "must be a YAML mapping" in str(exc.value)


def test_load_invalid_schema_raises_with_path(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("face: BAD FACE\nserver_name: x\n", encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        load_face_manifest(p)
    assert "failed schema validation" in str(exc.value)

# --------------------------------------------------------------------------
# Every checked-in face manifest must parse under the schema.
#
# Why this test exists: the schema tests above only build dicts in memory. A
# manifest added under faces/ could be malformed and nothing would notice until
# an operator ran import_faces.py against a live registry -- i.e. at the moment
# it changes what a paying customer can call. This closes that gap cheaply.
#
# It intentionally does NOT check closure against live backends: that needs the
# registry's server index and belongs to the importer.
# --------------------------------------------------------------------------
_FACES_DIR = Path(__file__).parent.parent.parent / "faces"


def _face_files() -> list[Path]:
    if not _FACES_DIR.is_dir():
        return []
    return sorted(_FACES_DIR.glob("*.yaml"))


def test_faces_dir_is_present():
    assert _FACES_DIR.is_dir(), f"faces/ not found at {_FACES_DIR}"
    assert _face_files(), "faces/ has no manifests -- the glob is wrong"


@pytest.mark.parametrize("path", _face_files(), ids=lambda p: p.stem)
def test_checked_in_face_parses(path: Path):
    manifest = load_face_manifest(path)
    assert manifest.tools, f"{path.name} declares no tools"
    aliases = [t.alias for t in manifest.tools]
    assert len(aliases) == len(set(aliases)), f"{path.name} has duplicate aliases"
    assert manifest.face == path.stem, (
        f"{path.name}: face name {manifest.face!r} must equal the file name"
    )


@pytest.mark.parametrize("path", _face_files(), ids=lambda p: p.stem)
def test_enabled_commercial_face_states_why(path: Path):
    """A commercial face may ship enabled, but not silently.

    Enabling one changes what every holder of its scope can call, and the
    manifest alone cannot know whether the backend's commercial surface and the
    scope grant are actually in place. So the rule is not "never enabled" --
    that would push operators to flip it in the console, where the decision
    leaves no trace in review. The rule is "justify it in the file": an enabled
    commercial face must carry the two facts a reviewer needs, the backend's
    commercial-surface evidence and who currently holds the grant.
    """
    manifest = load_face_manifest(path)
    if manifest.exposure != "commercial" or not manifest.is_enabled:
        return
    text = path.read_text(encoding="utf-8")
    assert "commercial_tools" in text or "commercial surface" in text, (
        f"{path.name}: enabling a commercial face requires naming the backend's "
        f"commercial-surface evidence in the manifest"
    )
    assert "SUB2API_KEY_CALLER_SCOPES" in text, (
        f"{path.name}: enabling a commercial face requires saying who holds the "
        f"scope grant (SUB2API_KEY_CALLER_SCOPES)"
    )
