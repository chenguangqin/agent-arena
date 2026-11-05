"""Arena 入口（Command Adapter v0.2）。不需要修改。

    uv run python agent_eval_entry.py --mode baseline < request.json
"""

import argparse
import sys
from pathlib import Path

from labkit.eval_entry import main

parser = argparse.ArgumentParser()
parser.add_argument("--mode", default="baseline")
parser.add_argument("--solution", action="store_true", help="run the reference solution instead of agent.py")
args = parser.parse_args()

if args.solution:
    from solution.agent import SKILLS_DIR, build_agent, on_session_end
else:
    from agent import build_agent, on_session_end

    SKILLS_DIR = Path("skills")

sys.exit(main(build_agent, on_session_end, args.mode, SKILLS_DIR))
