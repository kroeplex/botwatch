import asyncio
import json
import pytest
import re
import sys
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

    config, state, tokens = watch.call_args.args
    assert [source.key for source in config.sources] == ['slack:C0123456789']
    assert state == _state.StateFile()
    assert tokens['slack'] == 'xoxb-test'


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
    [None, b'', b'sources: [{type: slack}]', b'sources: [', b'sources: ["\xff"]'],
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


@pytest.mark.parametrize(
    ('config', 'message'),
    [
        (_CONFIG, 'set SLACK_TOKEN\n'),
        (
            'sources: [{type: github, repo: o/r, pull: 1}, {type: slack, conversation: C1}]',
            'set GITHUB_TOKEN and SLACK_TOKEN\n',
        ),
    ],
)
def test_main__fails_without_a_token(
    config: str,
    message: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / 'watch.yaml'
    path.write_text(config)
    monkeypatch.delenv('SLACK_TOKEN', raising=False)
    monkeypatch.delenv('GITHUB_TOKEN', raising=False)

    with pytest.raises(SystemExit) as exited:
        _cli.main(['--config', str(path)])

    assert exited.value.code == 2
    assert capsys.readouterr().err.endswith(message)


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


def test_main__suggests_a_command_for_a_typo(
    capsys: pytest.CaptureFixture[str],
) -> None:
    err = _fail(capsys, 'shwo', '--config', 'watch.yaml')

    assert "maybe you meant 'show'?" in err


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


class Test_main__add:
    def test__creates_the_config(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        path = tmp_path / 'watch.yaml'

        out, _ = _invoke(capsys, 'add', '--config', path, 'slack', 'C0123456789')

        assert out == 'added slack:C0123456789\n'
        assert (
            path.read_text() == 'sources:\n- type: slack\n  conversation: C0123456789\n'
        )

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
        'target',
        [
            'octo-org/octo-repo#26',
            'https://github.com/octo-org/octo-repo/pull/26',
            'https://github.com/octo-org/octo-repo/pull/26/files?w=1',
            'https://github.com/octo-org/octo-repo/pull/26#issuecomment-1',
        ],
    )
    def test__adds_a_pull_request(
        self,
        target: str,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        path = tmp_path / 'watch.yaml'

        out, _ = _invoke(capsys, 'add', '--config', path, 'github', target)

        assert out == 'added github:octo-org/octo-repo#26\n'
        assert path.read_text() == (
            'sources:\n- type: github\n  repo: octo-org/octo-repo\n  pull: 26\n'
        )

    @pytest.mark.parametrize(
        ('arguments', 'message'),
        [
            (['github', 'octo-org/octo-repo'], 'not a pull request'),
            (['github', 'octo-org/octo-repo#26', '--thread', '1'], 'only for slack'),
        ],
    )
    def test__fails_on_a_bad_pull_request(
        self,
        arguments: list[str],
        message: str,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        err = _fail(capsys, 'add', '--config', tmp_path / 'watch.yaml', *arguments)

        assert message in err

    def test__keeps_settings_not_given(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        path = tmp_path / 'watch.yaml'
        _invoke(
            capsys,
            'add',
            '--config',
            path,
            'slack',
            'C1',
            '--exclude',
            '{user: U0BOT}',
        )

        out, _ = _invoke(
            capsys,
            'add',
            '--config',
            path,
            'slack',
            'C1',
            '--poll-interval',
            '1m',
        )

        assert out == 'updated slack:C1\n'
        assert path.read_text() == (
            'sources:\n'
            '- type: slack\n'
            '  conversation: C1\n'
            '  poll_interval: 1m\n'
            '  exclude:\n'
            '  - user: U0BOT\n'
        )

    @pytest.mark.timeout(30)  # starts eight interpreters
    async def test__keeps_concurrent_additions(self, tmp_path: Path) -> None:
        path = tmp_path / 'watch.yaml'
        command = [sys.executable, '-m', 'botwatch', 'add', '--config', str(path)]

        processes = [
            await asyncio.create_subprocess_exec(*command, 'slack', f'C{number}')
            for number in range(8)
        ]

        assert [await process.wait() for process in processes] == [0] * 8
        config = _config.Config.load(path)
        assert sorted(source.key for source in config.sources) == [
            f'slack:C{number}' for number in range(8)
        ]

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


class Test_main__defaults:
    def test__sets_and_keeps_settings(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        path = tmp_path / 'watch.yaml'
        path.write_text(_CONFIG)

        added, _ = _invoke(
            capsys,
            'defaults',
            '--config',
            path,
            'slack',
            '--exclude',
            '{user: U0BOT}',
        )
        interval, _ = _invoke(
            capsys,
            'defaults',
            '--config',
            path,
            'slack',
            '--poll-interval',
            '15s',
        )
        unchanged, _ = _invoke(capsys, 'defaults', '--config', path, 'slack')
        shown, _ = _invoke(capsys, 'show', '--config', path)

        assert [added, interval, unchanged] == [
            'updated defaults for slack\n',
            'updated defaults for slack\n',
            'unchanged defaults for slack\n',
        ]
        assert path.read_text() == (
            'defaults:\n'
            '  slack:\n'
            '    poll_interval: 15s\n'
            '    exclude:\n'
            '    - user: U0BOT\n'
            'sources:\n'
            '- type: slack\n'
            '  conversation: C0123456789\n'
        )
        assert '  slack:C0123456789  every 15s\n' in shown

    def test__resets(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        path = tmp_path / 'watch.yaml'
        _invoke(
            capsys,
            'defaults',
            '--config',
            path,
            'slack',
            '--poll-interval',
            '15s',
            '--exclude',
            '{user: U0BOT}',
        )

        out, _ = _invoke(
            capsys,
            'defaults',
            '--config',
            path,
            'slack',
            '--reset',
            '--exclude',
            '{user: U0OTHER}',
        )

        assert out == 'updated defaults for slack\n'
        assert _config.Config.load(path).defaults.slack == _config.SlackDefaults(
            exclude=(slack.ExcludeRule(user='U0OTHER'),),
        )

    def test__are_kept_out_of_sources_and_shown(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        path = tmp_path / 'watch.yaml'
        _invoke(
            capsys,
            'defaults',
            '--config',
            path,
            'slack',
            '--poll-interval',
            '15s',
            '--exclude',
            '{user: U0BOT}',
        )
        _invoke(capsys, 'add', '--config', path, 'slack', 'C1')
        _invoke(
            capsys, 'add', '--config', path, 'slack', 'C1', '--exclude', '{text: x}'
        )

        pinned, _ = _invoke(
            capsys,
            'add',
            '--config',
            path,
            'slack',
            'C1',
            '--poll-interval',
            '30s',  # the built-in interval, pinned over the default
        )
        shown, _ = _invoke(capsys, 'show', '--config', path, '--json')

        assert pinned == 'updated slack:C1\n'
        assert _config.Config.load(path).sources == (
            _config.SlackSourceConfig(
                type='slack',
                conversation='C1',
                poll_interval=timedelta(seconds=30),
                exclude=(slack.ExcludeRule(text=re.compile('x')),),
            ),
        )
        document = json.loads(shown)
        assert document['defaults']['slack'] == {
            'poll_interval': '15s',
            'exclude': [
                {'user': 'U0BOT', 'bot_id': None, 'username': None, 'text': None},
            ],
        }
        assert document['sources'][0]['config']['poll_interval'] == '30s'
        assert [
            rule['user'] or rule['text']
            for rule in document['sources'][0]['config']['exclude']
        ] == ['U0BOT', 'x']

    def test__fails_on_a_bad_rule(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        path = tmp_path / 'watch.yaml'

        err = _fail(
            capsys, 'defaults', '--config', path, 'github', '--exclude', '{nope: 1}'
        )

        assert 'bad defaults' in err
        assert not path.exists()


class Test_main__remove:
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

    def test__reports_nothing_when_saving_fails(
        self,
        tmp_path: Path,
        mocker: MockerFixture,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        path = tmp_path / 'watch.yaml'
        path.write_text(_CONFIG)
        mocker.patch.object(_config.Config, 'save', side_effect=PermissionError())

        err = _fail(capsys, 'remove', '--config', path, 'slack:C0123456789')

        assert 'cannot save the config' in err
        assert not capsys.readouterr().out


class Test_main__show:
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
        path.write_text('{}')

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
            'defaults': {
                'slack': {'poll_interval': None, 'exclude': []},
                'github': {'poll_interval': None, 'exclude': []},
            },
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

    def test__notes_a_missing_state_file(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        config_path = tmp_path / 'watch.yaml'
        config_path.write_text(_CONFIG)
        state_path = tmp_path / 'state.yaml'

        out, err = _invoke(
            capsys,
            'show',
            '--config',
            config_path,
            '--state',
            state_path,
        )

        assert err == f'botwatch: no state file at {state_path} yet\n'
        assert 'starts from now on its next run' in out

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
