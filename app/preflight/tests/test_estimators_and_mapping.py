from app.preflight.adapters.openrouter_adapter import map_openrouter_model_to_supervea
from app.preflight.schemas.api_models import Message
from app.preflight.services.estimators import CostEstimator, TokenEstimator


def test_token_estimator_basic():
    estimator = TokenEstimator()
    messages = [Message(role="user", content="a" * 400)]  # 400 chars
    input_tokens, output_tokens = estimator.estimate_tokens(messages)
    assert input_tokens == 100  # 400 / 4
    assert output_tokens == 40  # 40% of input


def test_cost_estimator_basic():
    estimator = CostEstimator()
    route = {"input_price_per_1m": 1.0, "output_price_per_1m": 2.0}
    cost = estimator.estimate_cost_usd(route, input_tokens=1_000_000, output_tokens=500_000)
    assert cost == 1.0 + 1.0  # 1M in @ $1 + 0.5M out @ $2/1M


def test_openrouter_mapping_basic_model():
    model_obj = {
        "id": "openai/gpt-4o-mini",
        "pricing": {"prompt": "0.00000015", "completion": "0.0000006"},
        "context_length": 128000,
        "supported_parameters": ["tools"],
    }
    route = map_openrouter_model_to_supervea(model_obj)
    assert route.provider == "openai"
    assert route.model == "openai:gpt-4o-mini"
    assert "tool_use" in route.capabilities
    assert "long_context" in route.capabilities
    assert round(route.input_price_per_1m, 4) == 0.15
    assert round(route.output_price_per_1m, 4) == 0.60


def test_openrouter_mapping_unknown_provider_passthrough():
    model_obj = {
        "id": "someneworg/some-model",
        "pricing": {"prompt": "0.000001", "completion": "0.000002"},
        "context_length": 8000,
    }
    route = map_openrouter_model_to_supervea(model_obj)
    assert route.provider == "someneworg"
    assert route.model == "someneworg:some-model"
    assert "long_context" not in route.capabilities
