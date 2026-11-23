import pytest
from botwatch import _utils
from pathlib import Path


def test_write_atomically__cleans_up_after_a_failure(tmp_path: Path) -> None:
    path = tmp_path / 'state.yaml'
    path.write_text('previous')

    with pytest.raises(UnicodeEncodeError):
        _utils.files.write_atomically(path, '\ud800')  # unencodable

    assert [child.name for child in tmp_path.iterdir()] == ['state.yaml']
    assert path.read_text() == 'previous'


def test_write_atomically__keeps_the_permissions(tmp_path: Path) -> None:
    path = tmp_path / 'state.yaml'
    path.write_text('previous')
    path.chmod(0o640)

    _utils.files.write_atomically(path, 'next')

    assert path.read_text() == 'next'
    assert path.stat().st_mode & 0o777 == 0o640


def test_write_atomically__writes_through_a_symlink(tmp_path: Path) -> None:
    target = tmp_path / 'dotfiles' / 'watch.yaml'
    target.parent.mkdir()
    target.write_text('previous')
    link = tmp_path / 'watch.yaml'
    link.symlink_to(target)

    _utils.files.write_atomically(link, 'next')

    assert link.is_symlink()
    assert target.read_text() == 'next'
