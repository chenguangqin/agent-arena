import json

import pytest

from labkit import Env, trace


@pytest.fixture
def env(tmp_path):
    trace.configure(tmp_path / "trace.jsonl", echo=False)
    environment = Env.local()
    yield environment
    environment.close()


def events(tmp_path, name):
    path = tmp_path / "trace.jsonl"
    return [e for e in map(json.loads, path.read_text().splitlines()) if e["event"] == name] if path.exists() else []


class ModelQueue:
    """Hands out scripted models in creation order (patch build_model / MODEL_FACTORY with it)."""

    def __init__(self, models):
        self.models = list(models)

    def __call__(self, *args, **kwargs):
        return self.models.pop(0)
