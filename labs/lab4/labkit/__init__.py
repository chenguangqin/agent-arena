"""Lab infrastructure: support-desk environment, ticket queue, chat CLI, tracing, Arena entry.

Students do not need to read this package. The parts you use from agent.py:

    from labkit import Env, Session, build_model, run_cli
"""

from labkit.env import Env
from labkit.model import build_model
from labkit.session import Session, Trajectory
from labkit.cli import CommandContext, run_cli

__all__ = ["CommandContext", "Env", "Session", "Trajectory", "build_model", "run_cli"]
