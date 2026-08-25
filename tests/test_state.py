import datetime as dt
import json
from botwatch import _config
from botwatch import _state
from pathlib import Path

_T0 = dt.datetime(2026, 8, 1, 22, 10, tzinfo=dt.timezone.utc)
_T1 = dt.datetime(2026, 8, 2, 1, 12, tzinfo=dt.timezone.utc)
_T2 = dt.datetime(2026, 8, 2, 2, 30, tzinfo=dt.timezone.utc)


def _cfg(key: str = 'dev-slack', **options: object) -> _config.ChannelConfig:
    return _config.ChannelConfig(
        key=key, type='slack', options=options or {'conversation': 'C1'}
    )


def test_load_missing_file_starts_empty(tmp_path: Path) -> None:
    state = _state.State.load(tmp_path / 'nope.json', now=_T0)
    assert not state.channels


def test_resume_unknown_key_is_new() -> None:
    state = _state.State.load('/nonexistent', now=_T0)
    assert state.resume(_cfg()).case is _state.Case.NEW


def test_resume_returning_when_config_unchanged() -> None:
    state = _state.State.load('/nonexistent', now=_T0)
    state.record(_cfg(), now=_T0, driver={'cursor': '1'})
    result = state.resume(_cfg())
    assert result.case is _state.Case.RETURNING
    assert result.prev_state == {'cursor': '1'}


def test_resume_reconfigured_when_config_differs() -> None:
    state = _state.State.load('/nonexistent', now=_T0)
    state.record(_cfg(conversation='C1'), now=_T0, driver={'cursor': '1'})
    assert state.resume(_cfg(conversation='C2')).case is _state.Case.RECONFIGURED


def test_resume_unpositioned_after_a_failed_first_run() -> None:
    state = _state.State.load('/nonexistent', now=_T0)
    state.record(_cfg(), now=_T0, error='unreachable')
    result = state.resume(_cfg())
    assert result.case is _state.Case.UNPOSITIONED
    assert result.prev_state is None


def test_created_is_stamped_once() -> None:
    state = _state.State.load('/nonexistent', now=_T0)
    state.record(_cfg(), now=_T0, driver={'cursor': '1'})
    state.record(_cfg(), now=_T1, driver={'cursor': '2'})
    block = state.channels['dev-slack']
    assert block.created == _T0
    assert block.updated == _T1


def test_a_failure_keeps_the_prior_position_and_brackets_the_streak() -> None:
    state = _state.State.load('/nonexistent', now=_T0)
    state.record(_cfg(), now=_T0, driver={'cursor': '1'})
    state.record(_cfg(), now=_T1, error='boom')
    state.record(_cfg(), now=_T2, error='boom again')
    block = state.channels['dev-slack']
    assert block.driver == {'cursor': '1'}  # obligation 4: position untouched
    assert block.failing_since == _T1  # set on the first failure of the streak
    assert block.last_error_at == _T2  # refreshed each failure
    assert block.last_error == 'boom again'


def test_a_success_clears_the_error_streak() -> None:
    state = _state.State.load('/nonexistent', now=_T0)
    state.record(_cfg(), now=_T0, error='boom')
    state.record(_cfg(), now=_T1, driver={'cursor': '9'})
    block = state.channels['dev-slack']
    assert block.last_error is None
    assert block.failing_since is None


def test_retire_absent_retains_the_cursor_and_stamps_once() -> None:
    state = _state.State.load('/nonexistent', now=_T0)
    state.record(_cfg(), now=_T0, driver={'cursor': '1'})
    state.retire_absent(set(), now=_T1)
    state.retire_absent(set(), now=_T2)  # idempotent
    block = state.channels['dev-slack']
    assert block.retired == _T1
    assert block.driver == {'cursor': '1'}


def test_return_from_retirement_resumes_cursor_but_resets_health() -> None:
    state = _state.State.load('/nonexistent', now=_T0)
    state.record(_cfg(), now=_T0, error='boom')  # failing
    state.record(_cfg(), now=_T0, driver={'cursor': '5'})  # then positioned
    state.retire_absent(set(), now=_T1)
    # returns: still resumes from the retained cursor
    assert state.resume(_cfg()).prev_state == {'cursor': '5'}
    state.record(_cfg(), now=_T2, driver={'cursor': '6'})
    block = state.channels['dev-slack']
    assert block.retired is None
    assert block.created == _T0  # created preserved across retirement
    assert block.last_error is None  # health not carried forward


def test_save_is_atomic_and_round_trips(tmp_path: Path) -> None:
    path = tmp_path / 'state.json'
    state = _state.State.load(path, now=_T0)
    state.record(_cfg(), now=_T0, driver={'cursor': '1'}, last_event=_T0)
    state.save(path, now=_T1)
    assert not any(p.name.startswith('.state.json.') for p in tmp_path.iterdir())
    reloaded = _state.State.load(path, now=_T2)
    block = reloaded.channels['dev-slack']
    assert block.driver == {'cursor': '1'}
    assert block.last_event == _T0
    assert reloaded.resume(_cfg()).case is _state.Case.RETURNING


def test_to_document_is_pure_and_serialisable() -> None:
    state = _state.State.load('/nonexistent', now=_T0)
    state.record(_cfg(), now=_T0, driver={'cursor': '1'}, last_event=_T0)
    document = state.to_document(now=_T1)
    assert document['version'] == 1
    assert set(document) == {'version', 'created', 'updated', 'channels'}
    # round-trips through JSON with no custom encoder (already primitive)
    assert json.loads(json.dumps(document))['channels']['dev-slack']['driver'] == {
        'cursor': '1'
    }
