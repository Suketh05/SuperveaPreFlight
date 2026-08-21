"""Deterministic, cheap estimators used by the decision engine.

MVP intentionally uses simple heuristics rather than calling any LLM —
the Preflight layer must stay deterministic and fast (sub-10ms budget).
"""

from app.preflight.schemas.api_models import Message


class TokenEstimator:
    def estimate_tokens(self, messages: list[Message]) -> tuple[int, int]:
        """Return (estimated_input_tokens, estimated_output_tokens).

        MVP heuristic: ~4 characters per token, output guessed at 40% of
        input size. Refine later using real observations from route_observations.
        """
        total_chars = sum(len(m.content) for m in messages)
        estimated_input = max(1, total_chars // 4)
        estimated_output = int(estimated_input * 0.4)
        return estimated_input, estimated_output


class CostEstimator:
    def estimate_cost_usd(self, route: dict, input_tokens: int, output_tokens: int) -> float:
        in_price = float(route["input_price_per_1m"])
        out_price = float(route["output_price_per_1m"])
        est = (input_tokens / 1_000_000.0) * in_price
        est += (output_tokens / 1_000_000.0) * out_price
        return est
