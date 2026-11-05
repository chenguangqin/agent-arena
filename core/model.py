"""Bedrock model construction shared by the main agent and helper agents."""

from __future__ import annotations

from typing import Any

from botocore.config import Config as BotoConfig

from core.config import Config

# Bedrock error codes that are transient, including mid-stream errors raised by
# ConverseStream that botocore does not retry on its own.
TRANSIENT_ERROR_CODES = frozenset(
    {
        "internalserverexception",
        "serviceunavailableexception",
        "modelstreamerrorexception",
        "throttlingexception",
    }
)


def boto_config() -> BotoConfig:
    return BotoConfig(
        connect_timeout=10,
        read_timeout=300,
        retries={"max_attempts": 8, "mode": "adaptive"},
    )


def is_transient(exception: BaseException) -> bool:
    response = getattr(exception, "response", None)
    if not isinstance(response, dict):
        return False
    code = str(response.get("Error", {}).get("Code", ""))
    return code.lower() in TRANSIENT_ERROR_CODES


def build_model(config: Config, model_id: str | None = None, cache: bool = False) -> Any:
    """Create a BedrockModel.

    ``model_id`` overrides the profile (helper agents may use a smaller model);
    ``cache`` enables automatic prompt caching (Lab3 L3.7).
    """
    from strands.models import BedrockModel

    kwargs: dict[str, Any] = {
        "model_id": model_id or config.get("model", "id"),
        "region_name": config.get("model", "region"),
        "max_tokens": config.get("model", "max_tokens"),
        "boto_client_config": boto_config(),
    }
    if cache:
        from strands.models.model import CacheConfig

        kwargs["cache_config"] = CacheConfig(strategy="auto")
    return BedrockModel(**kwargs)


def build_retry_strategy() -> Any:
    from strands.event_loop._retry import ModelRetryStrategy

    class TransientErrorRetryStrategy(ModelRetryStrategy):
        """Retry Bedrock server errors in addition to throttling."""

        def is_retryable(self, exception: Exception) -> bool:
            return super().is_retryable(exception) or is_transient(exception)

    return TransientErrorRetryStrategy()
