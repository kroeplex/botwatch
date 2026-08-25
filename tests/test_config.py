import datetime as dt
import pydantic
import pytest
from botwatch import _config
from pathlib import Path

_SAMPLE = """
version: 1
defaults:
  quiet_timeout: 20m
  boarding_window: 2s
channels:
  - key: dev-slack
    type: slack
    conversation: C0123456789
    meta:
      notify: "#builds"
  - key: upstream-main
    type: git
    remote: git@github.com:example/project.git
    refs: [refs/heads/main]
"""


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / 'watch.yaml'
    path.write_text(text)
    return path


def test_load_config_reads_channels_defaults_and_meta(tmp_path: Path) -> None:
    config = _config.load_config(_write(tmp_path, _SAMPLE))
    assert config.defaults.quiet_timeout == dt.timedelta(minutes=20)
    assert config.defaults.boarding_window == dt.timedelta(seconds=2)
    assert [c.key for c in config.channels] == ['dev-slack', 'upstream-main']
    assert config.channels[0].meta == {'notify': '#builds'}


def test_driver_fields_go_to_options_untouched(tmp_path: Path) -> None:
    config = _config.load_config(_write(tmp_path, _SAMPLE))
    assert config.channels[0].options == {'conversation': 'C0123456789'}
    assert config.channels[1].options == {
        'remote': 'git@github.com:example/project.git',
        'refs': ['refs/heads/main'],
    }


def test_meta_is_not_swept_into_options(tmp_path: Path) -> None:
    config = _config.load_config(_write(tmp_path, _SAMPLE))
    assert 'notify' not in config.channels[0].options


@pytest.mark.parametrize(
    'text,expected',
    [
        ('500ms', dt.timedelta(milliseconds=500)),
        ('2s', dt.timedelta(seconds=2)),
        ('20m', dt.timedelta(minutes=20)),
        ('1h', dt.timedelta(hours=1)),
        ('7d', dt.timedelta(days=7)),
    ],
)
def test_durations_parse(text: str, expected: dt.timedelta) -> None:
    parsed = _config.Defaults.model_validate({'quiet_timeout': text})
    assert parsed.quiet_timeout == expected


def test_a_bad_duration_is_fatal(tmp_path: Path) -> None:
    bad = _SAMPLE.replace('quiet_timeout: 20m', 'quiet_timeout: soon')
    with pytest.raises(_config.ConfigError):
        _config.load_config(_write(tmp_path, bad))


def test_a_duplicate_key_is_a_hard_error(tmp_path: Path) -> None:
    dupe = _SAMPLE.replace('upstream-main', 'dev-slack')
    with pytest.raises(_config.ConfigError, match='duplicate'):
        _config.load_config(_write(tmp_path, dupe))


@pytest.mark.parametrize('key', ['ok', 'a.b_c-1', 'A' * 64])
def test_well_formed_keys_are_accepted(key: str) -> None:
    assert _config.ChannelConfig(key=key, type='slack').key == key


@pytest.mark.parametrize('key', ['.leading', 'has space', 'nope/slash', '', 'x' * 65])
def test_malformed_keys_are_rejected(key: str) -> None:
    with pytest.raises(pydantic.ValidationError):
        _config.ChannelConfig(key=key, type='slack')


def test_a_non_mapping_document_is_fatal(tmp_path: Path) -> None:
    with pytest.raises(_config.ConfigError, match='mapping'):
        _config.load_config(_write(tmp_path, '- just\n- a\n- list\n'))


def test_a_missing_file_is_fatal(tmp_path: Path) -> None:
    with pytest.raises(_config.ConfigError):
        _config.load_config(tmp_path / 'nope.yaml')


def test_version_is_carried_but_unenforced(tmp_path: Path) -> None:
    other = _SAMPLE.replace('version: 1', 'version: 99')
    assert _config.load_config(_write(tmp_path, other)).version == 99
