import json
import pytest
from . import _fake_slack
from botwatch import _cli
from botwatch import _config
from botwatch import _state
from botwatch import slack
from pathlib import Path
from pytest_mock import MockerFixture

_CONFIG = 'sources:\n  - type: slack\n    conversation: C0123456789\n'


def test_main(
    tmp_path: Path,
    mocker: MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / 'watch.yaml'
    path.write_text(_CONFIG)
    monkeypatch.setenv('SLACK_TOKEN', 'xoxb-test')
    watch = mocker.patch.object(_cli, '_watch', return_value=_state.StateFile())

    _cli.main(['--config', str(path)])

    config, state, token = watch.call_args.args
    assert [source.key for source in config.sources] == ['slack:C0123456789']
    assert state == _state.StateFile()
    assert token == 'xoxb-test'


def test_main__keeps_the_state(
    tmp_path: Path,
    mocker: MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / 'watch.yaml'
    config_path.write_text(_CONFIG)
    state_path = tmp_path / 'state.yaml'
    previous = _state.StateFile(
        sources={
            _config.SourceKey('slack:C0123456789'): slack.SlackState(
                after='1785600001.000000',
            ),
        },
    )
    previous.save(state_path)
    following = _state.StateFile(
        sources={
            _config.SourceKey('slack:C0123456789'): slack.SlackState(
                after='1785600002.000000',
            ),
        },
    )
    monkeypatch.setenv('SLACK_TOKEN', 'xoxb-test')
    watch = mocker.patch.object(_cli, '_watch', return_value=following)

    _cli.main(['--config', str(config_path), '--state', str(state_path)])

    assert watch.call_args.args[1] == previous
    assert _state.StateFile.load(state_path) == following


@pytest.mark.parametrize('state', [b'sources: [', b'sources: {"\xff": 1}'])
def test_main__fails_on_a_bad_state_file(
    state: bytes,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / 'watch.yaml'
    config_path.write_text(_CONFIG)
    state_path = tmp_path / 'state.yaml'
    state_path.write_bytes(state)
    monkeypatch.setenv('SLACK_TOKEN', 'xoxb-test')

    with pytest.raises(SystemExit) as exited:
        _cli.main(['--config', str(config_path), '--state', str(state_path)])

    assert exited.value.code == 2
    assert 'bad state file' in capsys.readouterr().err


@pytest.mark.parametrize(
    'config',
    [None, b'sources: [{type: slack}]', b'sources: [', b'sources: ["\xff"]'],
)
def test_main__fails_on_a_bad_config(
    config: bytes | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / 'watch.yaml'
    if config is not None:
        path.write_bytes(config)
    monkeypatch.setenv('SLACK_TOKEN', 'xoxb-test')

    with pytest.raises(SystemExit) as exited:
        _cli.main(['--config', str(path)])

    assert exited.value.code == 2
    assert 'bad config' in capsys.readouterr().err


def test_main__fails_without_a_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / 'watch.yaml'
    path.write_text(_CONFIG)
    monkeypatch.delenv('SLACK_TOKEN', raising=False)

    with pytest.raises(SystemExit) as exited:
        _cli.main(['--config', str(path)])

    assert exited.value.code == 2
    assert 'set SLACK_TOKEN' in capsys.readouterr().err


def test_main__writes_only_json_lines_to_stdout(
    tmp_path: Path,
    mocker: MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = _fake_slack.FakeSlack().create_client()
    mocker.patch.object(slack, 'SlackClient', return_value=client)
    path = tmp_path / 'watch.yaml'
    path.write_text(
        'quiet_timeout: 200ms\n'
        'linger: 0s\n'
        'sources: [{type: slack, conversation: C0MISSING}]\n',
    )
    monkeypatch.setenv('SLACK_TOKEN', 'xoxb-test')

    _cli.main(['--config', str(path)])

    lines = capsys.readouterr().out.splitlines()
    assert [json.loads(line)['event']['type'] for line in lines] == [
        'botwatch.source_failed',
    ]
    assert 'Traceback' in caplog.text
    assert 'channel_not_found' in caplog.text


def test_main__fails_on_a_state_file_it_cannot_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / 'watch.yaml'
    config_path.write_text(_CONFIG)
    monkeypatch.setenv('SLACK_TOKEN', 'xoxb-test')

    with pytest.raises(SystemExit) as exited:
        _cli.main(
            ['--config', str(config_path), '--state', str(tmp_path / 'no/state.yaml')],
        )

    assert exited.value.code == 2
    assert 'cannot write a state file' in capsys.readouterr().err


def test_main__fails_when_saving_fails(
    tmp_path: Path,
    mocker: MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / 'watch.yaml'
    config_path.write_text(_CONFIG)
    monkeypatch.setenv('SLACK_TOKEN', 'xoxb-test')
    mocker.patch.object(_cli, '_watch', return_value=_state.StateFile())
    mocker.patch.object(_state.StateFile, 'save', side_effect=OSError('disk full'))

    with pytest.raises(SystemExit) as exited:
        _cli.main(['--config', str(config_path), '--state', str(tmp_path / 's.yaml')])

    assert exited.value.code == 2
    assert 'disk full' in capsys.readouterr().err


def test_main__keeps_the_old_state_when_the_run_fails(
    tmp_path: Path,
    mocker: MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / 'watch.yaml'
    config_path.write_text(_CONFIG)
    state_path = tmp_path / 'state.yaml'
    previous = _state.StateFile(
        sources={
            _config.SourceKey('slack:C0123456789'): slack.SlackState(
                after='1785600001.000000',
            ),
        },
    )
    previous.save(state_path)
    monkeypatch.setenv('SLACK_TOKEN', 'xoxb-test')
    mocker.patch.object(_cli, '_watch', side_effect=BrokenPipeError())

    with pytest.raises(BrokenPipeError):
        _cli.main(['--config', str(config_path), '--state', str(state_path)])

    assert _state.StateFile.load(state_path) == previous
