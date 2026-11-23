import os
import shutil
import tempfile
from pathlib import Path


def write_atomically(
    path: Path,
    text: str,
) -> None:
    """Writes ``text`` to a temporary file beside ``path``, then renames it over
    ``path``, so a reader sees the old file or the new one, never a torn one.

    The temporary file is removed if anything fails, and an existing file's
    permissions carry over. A symlink is written through, to its target.
    """
    path = path.resolve()
    with tempfile.NamedTemporaryFile(
        'w',
        encoding='utf-8',
        dir=path.parent,
        prefix=f'.{path.name}.',
        delete_on_close=False,
    ) as temporary:
        temporary.write(text)
        temporary.flush()
        os.fsync(temporary.fileno())
        if path.exists():
            shutil.copymode(path, temporary.name)
        os.replace(temporary.name, path)
