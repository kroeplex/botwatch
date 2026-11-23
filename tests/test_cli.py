import json
import pytest
import re
from . import _fake_slack
from botwatch import _cli
from botwatch import _config
from botwatch import _state
from botwatch import slack
from datetime import timedelta
from pathlib import Path
from pytest_mock import MockerFixture

_CONFIG = 'sources:\n  - type: slack\n    conversation: C0123456789\n'


@pytest.mark.parametrize('command', [[], ['run']])
def test_main(
    command: list[str],
    tmp_path: Path,
    mocker: MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / 'watch.yaml'
    path.write_text(_CONFIG)
    monkeypatch.setenv('SLACK_TOKEN', 'xoxb-test')
    watch = mocker.patch.object(_cli, '_watch', return_value=_state.StateFile())

    _cli.main([*command, '--config', str(path)])

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


def _invoke(
    capsys: pytest.CaptureFixture[str],
    *arguments: str | Path,
) -> tuple[str, str]:
    _cli.main([str(argument) for argument in arguments])
    captured = capsys.readouterr()
    return captured.out, captured.err


def _fail(
    capsys: pytest.CaptureFixture[str],
    *arguments: str | Path,
) -> str:
    with pytest.raises(SystemExit) as exited:
        _cli.main([str(argument) for argument in arguments])
    assert exited.value.code == 2
    return capsys.readouterr().err


def _save_state(path: Path, **positions: str) -> None:
    _state.StateFile(
        sources={
            _config.SourceKey(key): slack.SlackState(after=after)
            for key, after in positions.items()
        },
    ).save(path)


class Test_add:
    def test__creates_the_config(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        path = tmp_path / 'watch.yaml'

        out, _ = _invoke(capsys, 'add', '--config', path, 'slack', 'C0123456789')

        assert out == 'added slack:C0123456789\n'
        assert path.read_text() == _CONFIG.replace('  - ', '- ').replace('    ', '  ')

    def test__adds_a_thread_with_settings(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        path = tmp_path / 'watch.yaml'
        path.write_text(_CONFIG)

        out, _ = _invoke(
            capsys,
            'add',
            '--config',
            path,
            'slack',
            'C0123456789',
            '--thread',
            '1785600000.000200',
            '--poll-interval',
            '15s',
            '--exclude',
            '{user: U0BOT}',
            '--exclude',
            "{text: '^\\[op 1\\]'}",
        )

        assert out == 'added slack:C0123456789/1785600000.000200\n'
        thread = _config.Config.load(path).sources[1]
        assert thread.poll_interval == timedelta(seconds=15)
        assert thread.exclude == (
            slack.ExcludeRule(user='U0BOT'),
            slack.ExcludeRule(text=re.compile(r'^\[op 1\]')),
        )

    def test__updates_a_source_in_place(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        path = tmp_path / 'watch.yaml'
        _invoke(capsys, 'add', '--config', path, 'slack', 'C1')
        _invoke(capsys, 'add', '--config', path, 'slack', 'C2')

        updated, _ = _invoke(
            capsys,
            'add',
            '--config',
            path,
            'slack',
            'C1',
            '--poll-interval',
            '1m',
        )
        unchanged, _ = _invoke(
            capsys,
            'add',
            '--config',
            path,
            'slack',
            'C1',
            '--poll-interval',
            '60s',
        )

        assert updated == 'updated slack:C1\n'
        assert unchanged == 'unchanged slack:C1\n'
        config = _config.Config.load(path)
        assert [source.key for source in config.sources] == ['slack:C1', 'slack:C2']
        assert config.sources[0].poll_interval == timedelta(minutes=1)

    @pytest.mark.parametrize(
        'option',
        [
            ['--thread', '1'],
            ['--poll-interval', 'soon'],
            ['--exclude', '{nope: 1}'],
            ['--exclude', '{}'],
            ['--exclude', '['],
            ['--exclude', 'U0BOT'],
        ],
    )
    def test__fails_on_a_bad_source(
        self,
        option: list[str],
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        path = tmp_path / 'watch.yaml'

        err = _fail(capsys, 'add', '--config', path, 'slack', 'C1', *option)

        assert 'bad source' in err
        assert not path.exists()

    def test__leaves_a_bad_config_alone(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        path = tmp_path / 'watch.yaml'
        path.write_text('sources: [')

        err = _fail(capsys, 'add', '--config', path, 'slack', 'C1')

        assert 'bad config' in err
        assert path.read_text() == 'sources: ['


class Test_remove:
    def test__removes_sources(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        path = tmp_path / 'watch.yaml'
        for conversation in ['C1', 'C2', 'C3']:
            _invoke(capsys, 'add', '--config', path, 'slack', conversation)

        out, err = _invoke(
            capsys,
            'remove',
            '--config',
            path,
            'slack:C1',
            'slack:C9',
            'slack:C3',
        )

        assert out == 'removed slack:C1\nremoved slack:C3\n'
        assert err == 'botwatch: not watched: slack:C9\n'
        config = _config.Config.load(path)
        assert [source.key for source in config.sources] == ['slack:C2']

    def test__leaves_the_config_alone_when_nothing_is_removed(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        path = tmp_path / 'watch.yaml'
        path.write_text(f'# watched by hand\n{_CONFIG}')

        _, err = _invoke(capsys, 'remove', '--config', path, 'slack:C9')

        assert err == 'botwatch: not watched: slack:C9\n'
        assert path.read_text() == f'# watched by hand\n{_CONFIG}'


class Test_show:
    def test__lists_the_sources(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        path = tmp_path / 'watch.yaml'
        _invoke(capsys, 'add', '--config', path, 'slack', 'C1')
        _invoke(
            capsys,
            'add',
            '--config',
            path,
            'slack',
            'C1',
            '--thread',
            '1785600000.000200',
        )

        out, _ = _invoke(capsys, 'show', '--config', path)

        assert out == (
            'quiet_timeout 20m, linger 5s\n'
            '  slack:C1                    every 30s\n'
            '  slack:C1/1785600000.000200  every 30s\n'
        )

    def test__shows_each_position(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        config_path = tmp_path / 'watch.yaml'
        _invoke(capsys, 'add', '--config', config_path, 'slack', 'C1')
        _invoke(capsys, 'add', '--config', config_path, 'slack', 'C2')
        _invoke(capsys, 'add', '--config', config_path, 'slack', 'C3')
        state_path = tmp_path / 'state.yaml'
        _save_state(
            state_path,
            **{
                'slack:C1': '1785600000.000200',
                'slack:C2': '0000000000.000000',
                'slack:C0OLD': '1785600000.000100',
            },
        )

        out, _ = _invoke(
            capsys,
            'show',
            '--config',
            config_path,
            '--state',
            state_path,
        )

        assert out == (
            'quiet_timeout 20m, linger 5s\n'
            '  slack:C1  every 30s  after 2026-08-01 16:00:00 UTC\n'
            '  slack:C2  every 30s  from the beginning\n'
            '  slack:C3  every 30s  starts from now on its next run\n'
            f'In {state_path} but no longer configured, dropped on the next run:\n'
            '  slack:C0OLD\n'
        )

    def test__an_empty_config(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        path = tmp_path / 'watch.yaml'
        path.touch()

        out, _ = _invoke(capsys, 'show', '--config', path)

        assert out == 'quiet_timeout 20m, linger 5s\n  no sources\n'

    def test__json(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        config_path = tmp_path / 'watch.yaml'
        _invoke(capsys, 'add', '--config', config_path, 'slack', 'C1')
        _invoke(
            capsys,
            'add',
            '--config',
            config_path,
            'slack',
            'C2',
            '--exclude',
            '{user: U0BOT}',
        )
        state_path = tmp_path / 'state.yaml'
        _save_state(
            state_path,
            **{'slack:C1': '1785600000.000200', 'slack:C0OLD': '0000000000.000000'},
        )

        out, _ = _invoke(
            capsys,
            'show',
            '--config',
            config_path,
            '--state',
            state_path,
            '--json',
        )

        assert json.loads(out) == {
            'quiet_timeout': '20m',
            'linger': '5s',
            'sources': [
                {
                    'key': 'slack:C1',
                    'config': {
                        'type': 'slack',
                        'conversation': 'C1',
                        'thread_ts': None,
                        'poll_interval': '30s',
                        'exclude': [],
                    },
                    'state': {'type': 'slack', 'after': '1785600000.000200'},
                },
                {
                    'key': 'slack:C2',
                    'config': {
                        'type': 'slack',
                        'conversation': 'C2',
                        'thread_ts': None,
                        'poll_interval': '30s',
                        'exclude': [
                            {
                                'user': 'U0BOT',
                                'bot_id': None,
                                'username': None,
                                'text': None,
                            },
                        ],
                    },
                    'state': None,
                },
            ],
            'unconfigured_state': ['slack:C0OLD'],
        }

    def test__fails_on_a_bad_state_file(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        config_path = tmp_path / 'watch.yaml'
        config_path.write_text(_CONFIG)
        state_path = tmp_path / 'state.yaml'
        state_path.write_text('sources: [')

        err = _fail(capsys, 'show', '--config', config_path, '--state', state_path)

        assert 'bad state file' in err
