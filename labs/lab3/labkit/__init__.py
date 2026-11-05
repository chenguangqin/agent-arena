"""Lab infrastructure: environment, chat CLI, tracing, Arena entry point.

Students do not need to read this package. The parts you use from agent.py:

    from labkit import Env, build_model, run_cli
"""

from labkit.env import Env
from labkit.model import build_model
from labkit.cli import run_cli

__all__ = ["Env", "build_model", "run_cli"]
