import datetime as dt
import json
import pytest
from botwatch import _cli
from botwatch import _driver
from botwatch import _registry
from botwatch._event import Event
from collections.abc import AsyncGenerator
from pathlib import Path

_CONFIG = """
defaults:
  quiet_timeout: 100ms
  boarding_window: 10ms
channels:
  - key: demo
    type: fake
"""


class _OneShotDriver:
    def __init__(self) -> None:
        self._done = False

    async def watch(self) -> AsyncGenerator[Event, None]:
        yield Event(
            channel='demo',
            type='fake',
            kind='message',
            id='42',
            ts=dt.datetime(2026, 8, 2, tzinfo=dt.timezone.utc),
        )
        self._done = True

    def get_state(self) -> dict[str, object]:
        return {'seen': self._done}


def _fake_registry() -> _driver.Registry:
    return {'fake': lambda key, cfg, pc, ps, ctx: _OneShotDriver()}


def test_main_runs_a_config_and_writes_state(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_registry, 'build_registry', _fake_registry)
    config = tmp_path / 'watch.yaml'
    config.write_text(_CONFIG)
    state = tmp_path / 'state.json'

    code = _cli.main(['--config', str(config), '--state', str(state)])

    assert code == 0
    emitted = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert emitted[0]['id'] == '42'
    assert state.exists()
    saved = json.loads(state.read_text())
    assert saved['channels']['demo']['driver'] == {'seen': True}


def test_main_reports_a_bad_config_and_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / 'watch.yaml'
    config.write_text('- not a mapping\n')
    code = _cli.main(['--config', str(config), '--state', str(tmp_path / 's.json')])
    assert code == 2
    assert 'mapping' in capsys.readouterr().err
