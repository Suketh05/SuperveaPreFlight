"""Proves the decision-cache-key bug is fixed: two requests with the same
tenant/model/policy_profile_id but different inline `constraints` must
NOT collide on the same cache key. Before the fix, `_decision_cache_key`
only hashed tenant_id/requested_model/policy_profile_id, so a second
request with different ad-hoc constraints (e.g.
allowed_providers=["made-up-provider"]) could be served the FIRST
request's cached decision, silently ignoring its own constraints.
"""

from app.preflight.schemas.api_models import PreflightConstraints
from app.preflight.schemas.internal_models import RequestProfile
from app.preflight.services.preflight_engine import PreflightEngine


def make_engine() -> PreflightEngine:
    # _decision_cache_key is pure w.r.t. these collaborators — none of
    # them are touched, so None stand-ins are fine for this unit test.
    return PreflightEngine(
        db=None,
        redis_cache=None,
        token_estimator=None,
        cost_estimator=None,
        route_filter=None,
    )


def make_profile(**overrides) -> RequestProfile:
    base = dict(
        operation="chat",
        requested_model="auto",
        estimated_input_tokens=5,
        estimated_output_tokens=5,
        capability="general_chat",
        region="global",
        data_classification="internal",
        tenant_id="demo-tenant",
        policy_profile_id=None,
    )
    base.update(overrides)
    return RequestProfile(**base)


def test_different_inline_constraints_produce_different_cache_keys():
    engine = make_engine()
    profile = make_profile()

    key_no_constraints = engine._decision_cache_key(profile, None)
    key_with_constraints = engine._decision_cache_key(
        profile, PreflightConstraints(allowed_providers=["made-up-provider"])
    )

    assert key_no_constraints != key_with_constraints


def test_two_different_nonempty_constraints_also_differ():
    engine = make_engine()
    profile = make_profile()

    key_a = engine._decision_cache_key(
        profile, PreflightConstraints(allowed_providers=["openai"])
    )
    key_b = engine._decision_cache_key(
        profile, PreflightConstraints(allowed_providers=["anthropic"])
    )

    assert key_a != key_b


def test_same_constraints_produce_the_same_key():
    engine = make_engine()
    profile = make_profile()

    key1 = engine._decision_cache_key(
        profile, PreflightConstraints(allowed_providers=["openai"], max_cost_usd=1.0)
    )
    key2 = engine._decision_cache_key(
        profile, PreflightConstraints(allowed_providers=["openai"], max_cost_usd=1.0)
    )

    assert key1 == key2


def test_none_constraints_is_stable_across_calls():
    # request.constraints=None (the common case: no inline constraints on
    # the request at all) must hash identically every time it's computed.
    engine = make_engine()
    profile = make_profile()

    key1 = engine._decision_cache_key(profile, None)
    key2 = engine._decision_cache_key(profile, None)

    assert key1 == key2
