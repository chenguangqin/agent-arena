from pathlib import Path

import pytest

from core.config import IMPL, ConfigError, load_profile

SCHEMA = {"completion": {"judge": ("off", IMPL), "judge_max_rounds": (3, int)}}


def _write(directory: Path, name: str, body: str) -> None:
    (directory / f"{name}.toml").write_text(body, encoding="utf-8")


def test_defaults_and_shared_sections(tmp_path, monkeypatch):
    monkeypatch.delenv("MODEL_ID", raising=False)
    _write(tmp_path, "baseline", "")
    config = load_profile("baseline", tmp_path, SCHEMA)
    assert not config.enabled("completion", "judge")
    assert config.get("limits", "max_turns") == 40
    assert config.get("limits", "max_total_tokens") == 0


def test_profile_inherits_and_overrides(tmp_path):
    _write(tmp_path, "base", "[limits]\nmax_turns = 10\n")
    _write(tmp_path, "child", 'extends = "base"\n[completion]\njudge = "hooks"\n')
    config = load_profile("child", tmp_path, SCHEMA)
    assert config.get("limits", "max_turns") == 10
    assert config.get("completion", "judge") == "hooks"


@pytest.mark.parametrize(
    "body",
    [
        '[completion]\njudge = "yes"\n',  # bad enum value
        '[completion]\njudgee = "hooks"\n',  # typo in key
        '[limits]\nmax_turns = "40"\n',  # wrong type
        "[limits]\nmax_turns = true\n",  # bool is not int
        "[nonsense]\nx = 1\n",  # unknown section
    ],
)
def test_invalid_profiles_are_rejected(tmp_path, body):
    _write(tmp_path, "bad", body)
    with pytest.raises(ConfigError):
        load_profile("bad", tmp_path, SCHEMA)


def test_inheritance_cycle_is_rejected(tmp_path):
    _write(tmp_path, "a", 'extends = "b"\n')
    _write(tmp_path, "b", 'extends = "a"\n')
    with pytest.raises(ConfigError, match="cycle"):
        load_profile("a", tmp_path, SCHEMA)


def test_lab_schema_cannot_redefine_shared_sections(tmp_path):
    _write(tmp_path, "baseline", "")
    with pytest.raises(ConfigError):
        load_profile("baseline", tmp_path, {"model": {"id": ("x", str)}})


def test_model_id_env_overrides_profile(tmp_path, monkeypatch):
    _write(tmp_path, "baseline", "")
    monkeypatch.setenv("MODEL_ID", "some-model")
    assert load_profile("baseline", tmp_path, SCHEMA).get("model", "id") == "some-model"
