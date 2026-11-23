import pydantic
import pytest
import re
from botwatch import _config
from botwatch import github
from botwatch import slack
from datetime import timedelta
from pathlib import Path


class Test_Config__load:
    def test__reads_yaml(self, tmp_path: Path) -> None:
        path = tmp_path / 'watch.yaml'
        path.write_text(
            'quiet_timeout: 55m\n'
            'linger: 500ms\n'
            'sources:\n'
            '  - type: slack\n'
            '    conversation: C0123456789\n'
            '  - type: slack\n'
            '    conversation: C0123456789\n'
            "    thread_ts: '1785600000.000200'\n"
            '    poll_interval: 15s\n'
            '    exclude:\n'
            '      - user: U0BOT\n'
            "      - text: '^\\[op 1\\]'\n",
        )

        config = _config.Config.load(path)

        assert config.quiet_timeout == timedelta(minutes=55)
        assert config.linger == timedelta(milliseconds=500)
        assert [source.key for source in config.sources] == [
            'slack:C0123456789',
            'slack:C0123456789/1785600000.000200',
        ]
        assert config.sources[1].poll_interval == timedelta(seconds=15)
        assert config.sources[1].exclude == (
            slack.ExcludeRule(user='U0BOT'),
            slack.ExcludeRule(text=re.compile(r'^\[op 1\]')),
        )

    def test__reads_json(self, tmp_path: Path) -> None:
        path = tmp_path / 'watch.json'
        path.write_text('{"sources": [{"type": "slack", "conversation": "C1"}]}')

        config = _config.Config.load(path)

        assert config.quiet_timeout == timedelta(minutes=20)
        assert [source.key for source in config.sources] == ['slack:C1']

    def test__reads_github_sources(self, tmp_path: Path) -> None:
        path = tmp_path / 'watch.yaml'
        path.write_text(
            'sources:\n'
            '  - type: github\n'
            '    repo: octo-org/octo-repo\n'
            '    pull: 26\n'
            '    exclude: [{user: octobot}]\n',
        )

        config = _config.Config.load(path)

        assert [source.key for source in config.sources] == [
            'github:octo-org/octo-repo#26',
        ]
        assert config.sources[0].poll_interval == timedelta(minutes=1)


@pytest.mark.parametrize(
    'document',
    [
        {'sources': [{'type': 'slack', 'conversation': 'C1'}] * 2},
        {'sources': [{'type': 'git', 'conversation': 'C1'}]},
        {'sources': [{'type': 'slack', 'conversation': 'C1', 'thread_ts': '1'}]},
        {'sources': [{'type': 'slack', 'conversation': 'C1', 'channel': 'C2'}]},
        {'linger': 'soon'},
        {'linger': 5},
        {'linger': 'PT5S'},
        {'quiet_timeout': '0s'},
        {'quiet_timeout': '99999999999h'},
        {'sources': [{'type': 'slack', 'conversation': 'general'}]},
        {'sources': [{'type': 'slack', 'conversation': 'C1/2'}]},
        {'sources': [{'type': 'slack', 'conversation': 'C1', 'poll_interval': '0s'}]},
        {'sources': [{'type': 'github', 'repo': 'octo-repo', 'pull': 1}]},
        {'sources': [{'type': 'github', 'repo': 'octo-org/octo-repo', 'pull': 0}]},
        {'sources': [{'type': 'github', 'repo': 'o/r', 'pull': 1, 'thread_ts': '1'}]},
        {'linger': '0.4ms'},
        {'linger': '1.0004s'},
    ],
)
def test_Config__rejects_invalid(document: dict[str, object]) -> None:
    with pytest.raises(pydantic.ValidationError):
        _config.Config.model_validate(document)


@pytest.mark.parametrize(
    'text',
    ['500ms', '1500ms', '5s', '90s', '55m', '1h', '25h', '0s'],
)
def test_format_duration__reads_back_unchanged(text: str) -> None:
    linger = _config.Config.model_validate({'linger': text}).linger

    assert _config.format_duration(linger) == text


def _slack(conversation: str, **settings: object) -> _config.SlackSourceConfig:
    return _config.SlackSourceConfig.model_validate(
        {'type': 'slack', 'conversation': conversation, **settings},
    )


def test_Config__with_source__appends_a_new_source() -> None:
    config = _config.Config(sources=(_slack('C1'),))

    assert config.with_source(_slack('C2')).sources == (_slack('C1'), _slack('C2'))


def test_Config__with_source__replaces_a_source_in_place() -> None:
    config = _config.Config(sources=(_slack('C1'), _slack('C2')))
    replacement = _slack('C1', poll_interval='5s')

    assert config.with_source(replacement).sources == (replacement, _slack('C2'))


def test_Config__without() -> None:
    config = _config.Config(sources=(_slack('C1'), _slack('C2'), _slack('C3')))

    assert config.without(
        [_config.SourceKey('slack:C1'), _config.SourceKey('slack:C3')],
    ).sources == (_slack('C2'),)


def test_Config__to_text__writes_just_the_settings_given() -> None:
    config = _config.Config(
        linger=timedelta(seconds=2),
        sources=(
            _slack('C1', poll_interval='30s'),  # the default, pinned
            _slack(
                'C1',
                thread_ts='1785600000.000200',
                poll_interval='15s',
                exclude=[{'user': 'U0BOT'}, {'text': '^\\[op 1\\]'}],
            ),
        ),
    )

    assert config.to_text() == (
        'linger: 2s\n'
        'sources:\n'
        '- type: slack\n'
        '  conversation: C1\n'
        '  poll_interval: 30s\n'
        '- type: slack\n'
        '  conversation: C1\n'
        "  thread_ts: '1785600000.000200'\n"
        '  poll_interval: 15s\n'
        '  exclude:\n'
        '  - user: U0BOT\n'
        '  - text: ^\\[op 1\\]\n'
    )


@pytest.mark.parametrize('name', ['watch.yaml', 'watch.json'])
def test_Config__save__reads_back_unchanged(name: str, tmp_path: Path) -> None:
    path = tmp_path / name
    config = _config.Config(
        quiet_timeout=timedelta(minutes=55),
        defaults=_config.Defaults(
            github=_config.GitHubDefaults(
                poll_interval=timedelta(milliseconds=1500),
                exclude=(github.ExcludeRule(text=re.compile('^🤖')),),
            ),
        ),
        sources=(
            _slack(
                'C1',
                thread_ts='1785600000.000200',
                poll_interval='1500ms',
                exclude=[
                    {'bot_id': 'B0BOT', 'text': '(?i)^bot:'},
                    {'text': '^🤖'},
                    {'user': 'a\x85b'},  # NEL, which YAML can misread as a line break
                ],
            ),
        ),
    )

    config.save(path)

    assert _config.Config.load(path) == config
    assert path.read_text().startswith('{' if name.endswith('.json') else 'quiet')


_DEFAULTS = _config.Defaults(
    slack=_config.SlackDefaults(
        poll_interval=timedelta(seconds=15),
        exclude=(slack.ExcludeRule(user='U0BOT'),),
    ),
)


def test_Config__resolve__applies_the_defaults() -> None:
    config = _config.Config(defaults=_DEFAULTS, sources=(_slack('C1'),))

    resolved = config.resolve(config.sources[0])

    assert resolved.key == 'slack:C1'
    assert resolved.poll_interval == timedelta(seconds=15)
    assert resolved.exclude == (slack.ExcludeRule(user='U0BOT'),)


def test_Config__resolve__keeps_the_sources_own_settings() -> None:
    source = _slack('C1', poll_interval='30s', exclude=[{'text': '^me'}])
    config = _config.Config(defaults=_DEFAULTS, sources=(source,))

    resolved = config.resolve(source)

    assert resolved.poll_interval == timedelta(seconds=30)
    assert resolved.exclude == (
        slack.ExcludeRule(user='U0BOT'),
        slack.ExcludeRule(text=re.compile('^me')),
    )


def test_Config__resolve__applies_each_type_its_own_defaults() -> None:
    github_defaults = _config.GitHubDefaults(
        poll_interval=timedelta(minutes=5),
        exclude=(github.ExcludeRule(user='octo-bot'),),
    )
    pull_request = _config.GitHubSourceConfig(type='github', repo='o/r', pull=1)
    config = _config.Config(
        defaults=_DEFAULTS.model_copy(update={'github': github_defaults}),
        sources=(_slack('C1'), pull_request),
    )

    resolved_slack = config.resolve(config.sources[0])
    resolved_github = config.resolve(pull_request)

    assert resolved_slack.exclude == (slack.ExcludeRule(user='U0BOT'),)
    assert resolved_slack.poll_interval == timedelta(seconds=15)
    assert resolved_github.exclude == (github.ExcludeRule(user='octo-bot'),)
    assert resolved_github.poll_interval == timedelta(minutes=5)


def test_Config__resolve__without_defaults() -> None:
    source = _config.GitHubSourceConfig(type='github', repo='o/r', pull=1)

    assert _config.Config(defaults=_DEFAULTS).resolve(source) == source


def test_Config__to_text__writes_just_the_defaults_given() -> None:
    config = _config.Config(
        defaults=_config.Defaults(
            github=_config.GitHubDefaults(
                exclude=(github.ExcludeRule(user='octo-bot'),),
            ),
        ),
    )

    assert config.to_text() == (
        'defaults:\n  github:\n    exclude:\n    - user: octo-bot\n'
    )
