from botwatch import _utils
from datetime import datetime
from datetime import timedelta
from datetime import timezone


def test_to_rfc3339() -> None:
    eastern = timezone(timedelta(hours=-5))

    assert _utils.datetime.to_rfc3339(
        datetime(2026, 8, 1, 21, 16, 16, 123456, tzinfo=eastern),
    ) == ('2026-08-02T02:16:16.123Z')
    assert _utils.datetime.to_rfc3339(datetime(2026, 8, 2, 2, 16, 16)) == (
        '2026-08-02T02:16:16.000Z'
    )
    assert _utils.datetime.to_rfc3339(None) is None


def test_from_rfc3339() -> None:
    assert _utils.datetime.from_rfc3339('2026-08-02T02:16:16Z') == datetime(
        2026,
        8,
        2,
        2,
        16,
        16,
        tzinfo=timezone.utc,
    )
    assert _utils.datetime.from_rfc3339('2026-08-02T02:16:16.123Z') == datetime(
        2026,
        8,
        2,
        2,
        16,
        16,
        123000,
        tzinfo=timezone.utc,
    )
