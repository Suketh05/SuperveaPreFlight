import pytest

from app.preflight.adapters.litellm_adapter import (
    MAX_REASONABLE_PRICE_PER_1M,
    map_litellm_model_to_supervea,
)


def test_litellm_mapping_basic_chat_model():
    model_obj = {
        "input_cost_per_token": 1.5e-07,
        "output_cost_per_token": 6e-07,
        "litellm_provider": "openai",
        "max_input_tokens": 128000,
        "max_output_tokens": 16384,
        "max_tokens": 16384,
        "mode": "chat",
        "supports_function_calling": True,
    }
    route = map_litellm_model_to_supervea("gpt-4o-mini", model_obj)
    assert route.provider == "openai"
    assert route.model == "openai:gpt-4o-mini"
    assert route.external_route_id == "litellm/gpt-4o-mini"
    assert "chat" in route.capabilities
    assert "tool_use" in route.capabilities
    assert "long_context" in route.capabilities
    assert round(route.input_price_per_1m, 4) == 0.15
    assert round(route.output_price_per_1m, 4) == 0.60


def test_litellm_mapping_embedding_model_is_skipped():
    model_obj = {
        "input_cost_per_token": 1e-08,
        "output_cost_per_token": 0.0,
        "litellm_provider": "openai",
        "mode": "embedding",
    }
    with pytest.raises(ValueError):
        map_litellm_model_to_supervea("text-embedding-3-small", model_obj)


def test_litellm_mapping_image_generation_model_is_skipped():
    model_obj = {
        "input_cost_per_token": 0.0,
        "output_cost_per_token": 0.0,
        "litellm_provider": "openai",
        "mode": "image_generation",
    }
    with pytest.raises(ValueError):
        map_litellm_model_to_supervea("dall-e-3", model_obj)


def test_litellm_mapping_sample_spec_is_skipped():
    model_obj = {
        "input_cost_per_token": 0.0,
        "output_cost_per_token": 0.0,
        "litellm_provider": "one of https://docs.litellm.ai/docs/providers",
        "mode": "one of: chat, embedding, completion, image_generation, ...",
    }
    with pytest.raises(ValueError):
        map_litellm_model_to_supervea("sample_spec", model_obj)


def test_litellm_mapping_missing_cost_fields_is_skipped():
    model_obj = {
        "litellm_provider": "openai",
        "mode": "chat",
    }
    with pytest.raises(ValueError):
        map_litellm_model_to_supervea("some-incomplete-model", model_obj)


def test_litellm_mapping_implausible_price_is_skipped():
    # A per-1M price above MAX_REASONABLE_PRICE_PER_1M after conversion —
    # mirrors the real "wandb/" catalog entries found while building this
    # adapter, which report costs that convert to well over 100,000/1M.
    implausible_input = (MAX_REASONABLE_PRICE_PER_1M + 1) / 1_000_000
    model_obj = {
        "input_cost_per_token": implausible_input,
        "output_cost_per_token": 1e-06,
        "litellm_provider": "wandb",
        "mode": "chat",
    }
    with pytest.raises(ValueError):
        map_litellm_model_to_supervea("wandb/some-bad-model", model_obj)


def test_litellm_mapping_tool_use_capability_present_when_supported():
    model_obj = {
        "input_cost_per_token": 1e-06,
        "output_cost_per_token": 2e-06,
        "litellm_provider": "bedrock",
        "mode": "chat",
        "supports_function_calling": True,
    }
    route = map_litellm_model_to_supervea("ai21.jamba-1-5-large-v1:0", model_obj)
    assert "tool_use" in route.capabilities


def test_litellm_mapping_tool_use_capability_absent_when_not_supported():
    model_obj = {
        "input_cost_per_token": 1.25e-05,
        "output_cost_per_token": 1.25e-05,
        "litellm_provider": "bedrock",
        "mode": "chat",
        # no supports_function_calling key at all — mirrors real entries
    }
    route = map_litellm_model_to_supervea("ai21.j2-mid-v1", model_obj)
    assert "tool_use" not in route.capabilities


def test_litellm_mapping_completion_mode_is_accepted():
    model_obj = {
        "input_cost_per_token": 1e-06,
        "output_cost_per_token": 2e-06,
        "litellm_provider": "openai",
        "mode": "completion",
    }
    route = map_litellm_model_to_supervea("gpt-3.5-turbo-instruct", model_obj)
    assert route.model == "openai:gpt-3.5-turbo-instruct"
