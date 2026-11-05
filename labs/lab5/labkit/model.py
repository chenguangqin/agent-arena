"""Model construction. Reads MODEL_ID / AWS_REGION from the environment."""

from __future__ import annotations

import os
from typing import Any

from botocore.config import Config as BotoConfig

DEFAULT_MODEL_ID = "global.anthropic.claude-haiku-4-5-20251001-v1:0"


def build_model(model_id: str | None = None, max_tokens: int = 4096, label: str = "main") -> Any:
    """A Bedrock model wrapped so the lab can count token usage per agent (``label``)."""
    from strands.models import BedrockModel

    from labkit import trace

    model = BedrockModel(
        model_id=model_id or os.environ.get("MODEL_ID", DEFAULT_MODEL_ID),
        region_name=os.environ.get("AWS_REGION", "us-west-2"),
        max_tokens=max_tokens,
        boto_client_config=BotoConfig(connect_timeout=10, read_timeout=300,
                                      retries={"max_attempts": 8, "mode": "adaptive"}),
    )
    return trace.tap(model, label)
