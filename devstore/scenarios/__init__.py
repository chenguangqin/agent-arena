"""Scenario registry.

A scenario is a package defining ``ID``, ``INSTRUCTION`` and ``build(seed) -> env``,
where ``env.tools()`` returns ``(name, description, function)`` tuples. It may
define ``describe(env)`` to summarise the final state for comparing runs.
Scenarios contain no scoring logic; scoring belongs to the Arena.
"""

from __future__ import annotations

import importlib
from types import ModuleType

_MODULES = ("intro_refund", "lab2_reconciliation", "lab3_checkout_incident", "lab4_ticket_queue", "lab5_promo_incident")


def get(scenario_id: str) -> ModuleType:
    for name in _MODULES:
        module = importlib.import_module(f"devstore.scenarios.{name}")
        if module.ID == scenario_id:
            return module
    raise KeyError(f"unknown scenario {scenario_id!r}; available: {available()}")


def available() -> list[str]:
    return sorted(importlib.import_module(f"devstore.scenarios.{n}").ID for n in _MODULES)
