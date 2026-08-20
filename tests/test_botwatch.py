import botwatch
import importlib.metadata


def test_version() -> None:
    assert botwatch.__version__ == importlib.metadata.version('botwatch')
