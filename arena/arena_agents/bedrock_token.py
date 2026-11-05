"""Print a short-term Bedrock API key (bearer token) derived from the host's AWS credentials.

Used to give the τ³ user simulator and verifier (which run inside task containers and
call models through LiteLLM) Bedrock access without copying AWS credentials into them.
The token only authorizes Bedrock calls and expires (default 2 hours).

    python -m arena_agents.bedrock_token [--region us-west-2] [--hours 2]
"""

from __future__ import annotations

import argparse
from datetime import timedelta


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", default="us-west-2")
    parser.add_argument("--hours", type=float, default=2.0)
    args = parser.parse_args()
    from aws_bedrock_token_generator import provide_token

    print(provide_token(region=args.region, expiry=timedelta(hours=args.hours)))


if __name__ == "__main__":
    main()
