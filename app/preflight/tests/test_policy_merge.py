"""Proves merge_constraints() combines a stored policy_profile with inline
request.constraints correctly, most-restrictive-of-each-field-wins. This is
the fix for the bug where inline constraints never reached hard_filter()
at all (see CLAUDE.md) — distinct from, and found after, the decision-cache
key bug fixed in test_decision_cache_key.py.
"""

from app.preflight.schemas.api_models import PreflightConstraints
from app.preflight.services.policy_merge import merge_constraints


def test_disjoint_allowed_providers_intersect_to_empty():
    base = PreflightConstraints(allowed_providers=["openai"])
    override = PreflightConstraints(allowed_providers=["anthropic"])

    result = merge_constraints(base, override)

    assert result.allowed_providers == []


def test_overlapping_allowed_providers_intersect():
    base = PreflightConstraints(allowed_providers=["openai", "anthropic"])
    override = PreflightConstraints(allowed_providers=["anthropic", "google"])

    result = merge_constraints(base, override)

    assert result.allowed_providers == ["anthropic"]


def test_only_override_has_allowed_providers_set():
    base = PreflightConstraints(allowed_providers=None)
    override = PreflightConstraints(allowed_providers=["made-up-provider"])

    result = merge_constraints(base, override)

    assert result.allowed_providers == ["made-up-provider"]


def test_only_base_has_max_cost_usd_set():
    base = PreflightConstraints(max_cost_usd=1.0)
    override = PreflightConstraints(max_cost_usd=None)

    result = merge_constraints(base, override)

    assert result.max_cost_usd == 1.0


def test_both_have_max_cost_usd_smaller_override_wins():
    base = PreflightConstraints(max_cost_usd=1.0)
    override = PreflightConstraints(max_cost_usd=0.5)

    result = merge_constraints(base, override)

    assert result.max_cost_usd == 0.5


def test_both_have_max_cost_usd_smaller_base_wins():
    base = PreflightConstraints(max_cost_usd=0.5)
    override = PreflightConstraints(max_cost_usd=1.0)

    result = merge_constraints(base, override)

    assert result.max_cost_usd == 0.5


def test_data_classification_restricted_beats_internal():
    base = PreflightConstraints(data_classification="internal")
    override = PreflightConstraints(data_classification="restricted")

    result = merge_constraints(base, override)

    assert result.data_classification == "restricted"


def test_data_classification_sensitive_stays_over_public():
    base = PreflightConstraints(data_classification="sensitive")
    override = PreflightConstraints(data_classification="public")

    result = merge_constraints(base, override)

    assert result.data_classification == "sensitive"


def test_override_none_returns_base_unchanged():
    base = PreflightConstraints(
        allowed_providers=["openai"], max_cost_usd=1.0, data_classification="sensitive"
    )

    result = merge_constraints(base, None)

    assert result == base
