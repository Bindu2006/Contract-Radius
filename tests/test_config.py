import pytest

from contractradius.config import Config, load_config
from contractradius.exceptions import ConfigError


def test_missing_config_file_returns_defaults(tmp_path):
    cfg = load_config(tmp_path / "nope.yaml")
    assert cfg == Config()
    assert "axios" in cfg.client_names


def test_valid_config_is_loaded(tmp_path):
    f = tmp_path / ".contractradius.yaml"
    f.write_text("client_names: [axios, myClient]\nignore_paths: ['/internal/*']\n"
                 "min_confidence: 0.5\nfail_on: none\n")
    cfg = load_config(f)
    assert cfg.client_names == ["axios", "myClient"]
    assert cfg.min_confidence == 0.5
    assert cfg.fail_on == "none"


def test_empty_file_returns_defaults(tmp_path):
    f = tmp_path / "c.yaml"
    f.write_text("")
    assert load_config(f) == Config()


def test_invalid_yaml_raises_config_error(tmp_path):
    f = tmp_path / "c.yaml"
    f.write_text("client_names: [unclosed")
    with pytest.raises(ConfigError):
        load_config(f)


def test_non_mapping_yaml_raises_config_error(tmp_path):
    f = tmp_path / "c.yaml"
    f.write_text("- just\n- a list\n")
    with pytest.raises(ConfigError):
        load_config(f)


def test_unknown_key_and_bad_value_raise_config_error(tmp_path):
    f = tmp_path / "c.yaml"
    f.write_text("min_confidense: 0.5\n")
    with pytest.raises(ConfigError):
        load_config(f)
    f.write_text("fail_on: sometimes\n")
    with pytest.raises(ConfigError):
        load_config(f)
