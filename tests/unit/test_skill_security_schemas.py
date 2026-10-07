"""
Property-based tests for skill security schema round-trip serialization.

# Feature: skill-scanner-integration, Property 1: Schema model round-trip serialization

**Validates: Requirements 2.5, 9.3**
"""

from hypothesis import given, settings
from hypothesis import strategies as st

from registry.schemas.skill_security import (
    SkillSecurityScanConfig,
    SkillSecurityScanFinding,
    SkillSecurityScanResult,
    SkillSecurityStatus,
)

VALID_SEVERITIES = ["CRITICAL", "HIGH", "MEDIUM", "LOW"]
VALID_ANALYZERS = ["static", "behavioral", "llm", "meta", "virustotal", "ai-defense"]
VALID_SCAN_STATUSES = ["pending", "completed", "failed"]


def _finding_strategy():
    """Strategy for generating valid SkillSecurityScanFinding instances."""
    return st.builds(
        SkillSecurityScanFinding,
        file_path=st.one_of(st.none(), st.text(min_size=1, max_size=100)),
        line_number=st.one_of(st.none(), st.integers(min_value=0, max_value=10000)),
        severity=st.sampled_from(VALID_SEVERITIES),
        threat_names=st.lists(st.text(min_size=1, max_size=50), max_size=5),
        threat_summary=st.text(max_size=200),
        analyzer=st.sampled_from(VALID_ANALYZERS),
        is_safe=st.booleans(),
    )


def _scan_result_strategy():
    """Strategy for generating valid SkillSecurityScanResult instances."""
    return st.builds(
        SkillSecurityScanResult,
        skill_path=st.text(min_size=1, max_size=100),
        skill_md_url=st.one_of(st.none(), st.text(min_size=1, max_size=200)),
        scan_timestamp=st.text(min_size=1, max_size=50),
        is_safe=st.booleans(),
        critical_issues=st.integers(min_value=0, max_value=100),
        high_severity=st.integers(min_value=0, max_value=100),
        medium_severity=st.integers(min_value=0, max_value=100),
        low_severity=st.integers(min_value=0, max_value=100),
        analyzers_used=st.lists(st.sampled_from(VALID_ANALYZERS), max_size=6),
        raw_output=st.fixed_dictionaries({}),
        output_file=st.one_of(st.none(), st.text(min_size=1, max_size=100)),
        scan_failed=st.booleans(),
        error_message=st.one_of(st.none(), st.text(min_size=1, max_size=200)),
    )


def _scan_config_strategy():
    """Strategy for generating valid SkillSecurityScanConfig instances."""
    return st.builds(
        SkillSecurityScanConfig,
        enabled=st.booleans(),
        scan_on_registration=st.booleans(),
        block_unsafe_skills=st.booleans(),
        analyzers=st.text(min_size=1, max_size=50),
        scan_timeout_seconds=st.integers(min_value=1, max_value=600),
        llm_api_key=st.one_of(st.none(), st.text(min_size=1, max_size=100)),
        virustotal_api_key=st.one_of(st.none(), st.text(min_size=1, max_size=100)),
        ai_defense_api_key=st.one_of(st.none(), st.text(min_size=1, max_size=100)),
        add_security_pending_tag=st.booleans(),
    )


def _security_status_strategy():
    """Strategy for generating valid SkillSecurityStatus instances."""
    return st.builds(
        SkillSecurityStatus,
        skill_path=st.text(min_size=1, max_size=100),
        skill_name=st.text(min_size=1, max_size=100),
        is_safe=st.booleans(),
        last_scan_timestamp=st.one_of(st.none(), st.text(min_size=1, max_size=50)),
        critical_issues=st.integers(min_value=0, max_value=100),
        high_severity=st.integers(min_value=0, max_value=100),
        scan_status=st.sampled_from(VALID_SCAN_STATUSES),
        is_disabled_for_security=st.booleans(),
    )


class TestSkillSecuritySchemaRoundTrip:
    """Property 1: Schema model round-trip serialization."""

    @given(finding=_finding_strategy())
    @settings(max_examples=100)
    def test_finding_round_trip(self, finding: SkillSecurityScanFinding):
        """Serializing and reconstructing a SkillSecurityScanFinding produces an equal object."""
        dumped = finding.model_dump()
        reconstructed = SkillSecurityScanFinding(**dumped)
        assert reconstructed == finding

    @given(result=_scan_result_strategy())
    @settings(max_examples=100)
    def test_scan_result_round_trip(self, result: SkillSecurityScanResult):
        """Serializing and reconstructing a SkillSecurityScanResult produces an equal object."""
        dumped = result.model_dump()
        reconstructed = SkillSecurityScanResult(**dumped)
        assert reconstructed == result

    @given(config=_scan_config_strategy())
    @settings(max_examples=100)
    def test_scan_config_round_trip(self, config: SkillSecurityScanConfig):
        """Serializing and reconstructing a SkillSecurityScanConfig produces an equal object."""
        dumped = config.model_dump()
        reconstructed = SkillSecurityScanConfig(**dumped)
        assert reconstructed == config

    @given(status=_security_status_strategy())
    @settings(max_examples=100)
    def test_security_status_round_trip(self, status: SkillSecurityStatus):
        """Serializing and reconstructing a SkillSecurityStatus produces an equal object."""
        dumped = status.model_dump()
        reconstructed = SkillSecurityStatus(**dumped)
        assert reconstructed == status
