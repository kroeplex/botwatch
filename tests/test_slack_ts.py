import pydantic
import pytest
from botwatch.slack import _ts
from datetime import datetime
from datetime import timezone


def test_ts_to_datetime() -> None:
    assert _ts.ts_to_datetime('1785633376.000200') == datetime(
        2026,
        8,
        2,
        1,
        16,
        16,
        200,
        tzinfo=timezone.utc,
    )


@pytest.mark.parametrize('ts', ['1785633376', '1785633376.2', 'abc.000000'])
def test_SlackTs__rejects_malformed(ts: str) -> None:
    with pytest.raises(pydantic.ValidationError):
        pydantic.TypeAdapter(_ts.SlackTs).validate_python(ts)
