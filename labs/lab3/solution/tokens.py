"""Rough token estimates for text we have not sent yet.

Exact counts only exist after a model call (usage). Before that we estimate:
about 4 ASCII characters per token, and about 1.5 CJK characters per token.
/context prints the last real input token count next to the estimate so you can
see how far off it is.
"""

from __future__ import annotations

import json
from typing import Any


def estimate(value: Any) -> int:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    return int(ascii_chars / 4 + (len(text) - ascii_chars) / 1.5)
