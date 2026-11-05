"""Arena 入口（Command Adapter v0.2）。不需要修改。

    uv run python agent_eval_entry.py --mode baseline < request.json
"""

import argparse
import sys

from labkit.eval_entry import main

parser = argparse.ArgumentParser()
parser.add_argument("--mode", default="baseline")
parser.add_argument("--solution", action="store_true", help="run the reference solution instead of agent.py")
args = parser.parse_args()

if args.solution:
    from solution.agent import build_agent
else:
    from agent import build_agent

sys.exit(main(build_agent, args.mode))
