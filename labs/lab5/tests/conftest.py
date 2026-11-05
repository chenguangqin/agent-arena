import pytest

from labkit import Env, trace


@pytest.fixture
def env(tmp_path):
    trace.configure(tmp_path / "trace.jsonl", echo=False)
    environment = Env.local()
    yield environment
    environment.close()


def events(tmp_path, name):
    import json

    path = tmp_path / "trace.jsonl"
    return [e for e in map(json.loads, path.read_text().splitlines()) if e["event"] == name] if path.exists() else []
