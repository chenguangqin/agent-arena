"""Profile loading and validation.

Each lab declares a schema for its feature switches on top of the shared
``model`` and ``limits`` sections. Unknown sections, keys, or values are
errors, so a typo cannot silently turn a feature off.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

OFF = "off"
ON = (OFF, "on")
IMPL = (OFF, "hooks", "sdk")

# section -> key -> (default, allowed values tuple | python type)
Schema = dict[str, dict[str, tuple[Any, Any]]]

BASE_SCHEMA: Schema = {
    "model": {
        "id": ("global.anthropic.claude-haiku-4-5-20251001-v1:0", str),
        "region": ("us-west-2", str),
        "max_tokens": (4096, int),
    },
    "limits": {
        "max_turns": (40, int),
        # Budget across every invocation and helper agent in a run; 0 = unlimited.
        "max_total_tokens": (0, int),
    },
}


class ConfigError(ValueError):
    """Raised when a profile is malformed."""


@dataclass(frozen=True)
class Config:
    profile: str
    values: dict[str, dict[str, Any]] = field(default_factory=dict)

    def get(self, section: str, key: str) -> Any:
        return self.values[section][key]

    def enabled(self, section: str, key: str) -> bool:
        return self.values.get(section, {}).get(key, OFF) not in (OFF, False)

    def as_dict(self) -> dict[str, dict[str, Any]]:
        return {section: dict(keys) for section, keys in self.values.items()}


def merge_schema(lab_schema: Schema) -> Schema:
    overlap = set(lab_schema) & set(BASE_SCHEMA)
    if overlap:
        raise ConfigError(f"lab schema may not redefine shared sections: {sorted(overlap)}")
    return {**BASE_SCHEMA, **lab_schema}


def _defaults(schema: Schema) -> dict[str, dict[str, Any]]:
    return {section: {k: spec[0] for k, spec in keys.items()} for section, keys in schema.items()}


def _validate(schema: Schema, section: str, key: str, value: Any) -> Any:
    if section not in schema:
        raise ConfigError(f"unknown section [{section}]")
    if key not in schema[section]:
        raise ConfigError(f"unknown key {section}.{key}")
    _, allowed = schema[section][key]
    if isinstance(allowed, tuple):
        if value not in allowed:
            raise ConfigError(f"{section}.{key} must be one of {allowed}, got {value!r}")
    elif not isinstance(value, allowed) or (isinstance(value, bool) and allowed is not bool):
        raise ConfigError(f"{section}.{key} must be {allowed.__name__}, got {value!r}")
    return value


def _read(name: str, directory: Path, schema: Schema, seen: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    if name in seen:
        raise ConfigError(f"profile inheritance cycle: {' -> '.join((*seen, name))}")
    path = directory / f"{name}.toml"
    if not path.is_file():
        raise ConfigError(f"profile not found: {path}")
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    parent = data.pop("extends", None)
    merged = _read(parent, directory, schema, (*seen, name)) if parent else _defaults(schema)
    for section, keys in data.items():
        if not isinstance(keys, dict):
            raise ConfigError(f"[{section}] must be a table")
        for key, value in keys.items():
            merged[section][key] = _validate(schema, section, key, value)
    return merged


def load_profile(name: str, directory: Path, lab_schema: Schema | None = None) -> Config:
    """Load ``directory/<name>.toml``; ``MODEL_ID`` / ``AWS_REGION`` override the model."""
    values = _read(name, directory, merge_schema(lab_schema or {}), ())
    if model_id := os.environ.get("MODEL_ID"):
        values["model"]["id"] = model_id
    if region := os.environ.get("AWS_REGION"):
        values["model"]["region"] = region
    return Config(profile=name, values=values)


def list_profiles(directory: Path) -> list[str]:
    return sorted(p.stem for p in directory.glob("*.toml"))
