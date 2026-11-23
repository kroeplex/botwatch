import pydantic
import pytest
from botwatch import _config
from botwatch import _state
from botwatch import slack
from pathlib import Path

_STATE = _state.StateFile(
    sources={
        _config.SourceKey('slack:C1'): slack.SlackState(after='1785600001.000000'),
        _config.SourceKey('slack:C1/1785600000.000100'): slack.SlackState(
            after='1785600002.000000',
        ),
    },
)


def test_StateFile__round_trips_through_yaml() -> None:
    text = _STATE.to_yaml()

    assert text == (
        'sources:\n'
        '  slack:C1:\n'
        '    type: slack\n'
        "    after: '1785600001.000000'\n"
        '  slack:C1/1785600000.000100:\n'
        '    type: slack\n'
        "    after: '1785600002.000000'\n"
    )
    assert _state.StateFile.from_yaml(text) == _STATE


@pytest.mark.parametrize(
    'text',
    [
        "sources: {'slack:C1': {type: git, after: '1785600001.000000'}}",
        "sources: {'slack:C1': {type: slack, after: 'soon'}}",
        'cursors: {}',
    ],
)
def test_StateFile__from_yaml__rejects_invalid(text: str) -> None:
    with pytest.raises(pydantic.ValidationError):
        _state.StateFile.from_yaml(text)


def test_StateFile__load__treats_a_missing_file_as_empty(tmp_path: Path) -> None:
    assert _state.StateFile.load(tmp_path / 'state.yaml') == _state.StateFile()


def test_StateFile__save(tmp_path: Path) -> None:
    path = tmp_path / 'state.yaml'
    path.write_text('stale')

    _STATE.save(path)

    assert _state.StateFile.load(path) == _STATE
    assert [child.name for child in tmp_path.iterdir()] == ['state.yaml']
