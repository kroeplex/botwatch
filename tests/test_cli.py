import pytest
from botwatch import _cli
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
    watch = mocker.patch.object(_cli, '_watch')

    _cli.main(['--config', str(path)])

    config, token = watch.call_args.args
    assert [source.key for source in config.sources] == ['slack:C0123456789']
    assert token == 'xoxb-test'


@pytest.mark.parametrize('config', [None, 'sources: [{type: slack}]', 'sources: ['])
def test_main__fails_on_a_bad_config(
    config: str | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / 'watch.yaml'
    if config is not None:
        path.write_text(config)
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
