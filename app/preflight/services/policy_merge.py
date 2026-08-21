"""Merges a stored policy_profile with inline per-request constraints.

PreflightRequest can specify policy two ways: a stored `policy_profile_id`
(a tenant's standing rules, loaded via queries.load_policy) and inline
`constraints` sent directly in the request body (a one-off request's extra
restrictions). Both must be honored together — this module combines them
into a single PreflightConstraints before route_filter.hard_filter() ever
sees it, using a "most restrictive of each field wins" merge so the result
is never looser than either input alone.
"""

from typing import Optional

from app.preflight.schemas.api_models import PreflightConstraints

_DATA_CLASSIFICATION_RANK = {"public": 0, "internal": 1, "sensitive": 2, "restricted": 3}


def merge_constraints(
    base: PreflightConstraints, override: Optional[PreflightConstraints]
) -> PreflightConstraints:
    """Merge two constraint sets. The result is always at least as
    restrictive as either input alone — never looser. `base` is typically
    the stored policy_profile (a tenant's standing rules); `override` is
    typically inline request.constraints (a one-off request's extra
    restrictions). If override is None, base is returned unchanged.
    """
    if override is None:
        return base

    return PreflightConstraints(
        allowed_providers=_merge_list(base.allowed_providers, override.allowed_providers),
        allowed_models=_merge_list(base.allowed_models, override.allowed_models),
        allowed_regions=_merge_list(base.allowed_regions, override.allowed_regions),
        required_capabilities=_merge_list(
            base.required_capabilities, override.required_capabilities
        ),
        max_cost_usd=_merge_min(base.max_cost_usd, override.max_cost_usd),
        max_latency_ms=_merge_min(base.max_latency_ms, override.max_latency_ms),
        data_classification=_more_restrictive_classification(
            base.data_classification, override.data_classification
        ),
    )


def _merge_list(base: Optional[list], override: Optional[list]) -> Optional[list]:
    if base is not None and override is not None:
        return list(set(base) & set(override))
    if base is not None:
        return base
    return override


def _merge_min(base, override):
    if base is not None and override is not None:
        return min(base, override)
    if base is not None:
        return base
    return override


def _more_restrictive_classification(base: Optional[str], override: Optional[str]) -> str:
    # data_classification defaults to "internal" rather than None, so both
    # sides normally have a value; an explicit None is treated as the same
    # "internal" default rather than as "unrestricted".
    base_rank = _DATA_CLASSIFICATION_RANK.get(base, _DATA_CLASSIFICATION_RANK["internal"])
    override_rank = _DATA_CLASSIFICATION_RANK.get(override, _DATA_CLASSIFICATION_RANK["internal"])
    return base if base_rank >= override_rank else override
