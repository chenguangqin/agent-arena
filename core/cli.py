"""Command line shared by every lab: ``python -m labN chat|run``."""

from __future__ import annotations

import argparse

from core.config import list_profiles
from core.lab import Lab


def main(lab: Lab, argv: list[str] | None = None) -> int:
    profiles = list_profiles(lab.profiles_dir)
    parser = argparse.ArgumentParser(prog=f"python -m {lab.name}", description=lab.title)
    sub = parser.add_subparsers(dest="command", required=True)

    chat = sub.add_parser("chat", help="interactive session against a local devstore scenario")
    chat.add_argument("--profile", default="baseline", choices=profiles)
    chat.add_argument("--scenario", default=lab.scenario)
    chat.add_argument("--seed", type=int, default=0)
    chat.add_argument("-v", "--verbose", action="store_true", help="also print tool results")

    run = sub.add_parser("run", help="Command Adapter v0.2: one JSON request on stdin")
    run.add_argument("--profile", default="baseline", choices=profiles)

    args = parser.parse_args(argv)
    if args.command == "run":
        from core.protocol import main as protocol_main

        return protocol_main(lab, args.profile)
    from core.chat import run_chat

    run_chat(lab, args.profile, args.scenario, args.seed, args.verbose)
    return 0


