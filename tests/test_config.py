import pydantic
import pytest
from botwatch import _config
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
            '    poll_interval: 15s\n',
        )

        config = _config.Config.load(path)

        assert config.quiet_timeout == timedelta(minutes=55)
        assert config.linger == timedelta(milliseconds=500)
        assert [source.key for source in config.sources] == [
            'slack:C0123456789',
            'slack:C0123456789/1785600000.000200',
        ]
        assert config.sources[1].poll_interval == timedelta(seconds=15)

    def test__reads_json(self, tmp_path: Path) -> None:
        path = tmp_path / 'watch.json'
        path.write_text('{"sources": [{"type": "slack", "conversation": "C1"}]}')

        config = _config.Config.load(path)

        assert config.quiet_timeout == timedelta(minutes=20)
        assert [source.key for source in config.sources] == ['slack:C1']


@pytest.mark.parametrize(
    'document',
    [
        {'sources': [{'type': 'slack', 'conversation': 'C1'}] * 2},
        {'sources': [{'type': 'git', 'conversation': 'C1'}]},
        {'sources': [{'type': 'slack', 'conversation': 'C1', 'thread_ts': '1'}]},
        {'sources': [{'type': 'slack', 'conversation': 'C1', 'channel': 'C2'}]},
        {'linger': 'soon'},
    ],
)
def test_Config__rejects_invalid(document: dict[str, object]) -> None:
    with pytest.raises(pydantic.ValidationError):
        _config.Config.model_validate(document)
