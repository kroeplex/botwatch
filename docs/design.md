# botwatch design

How botwatch works now, for someone who has never seen it. This document
describes the code, and the code wins any disagreement; a PR that changes what
is described here updates it. For the reasoning that led here, see the design
proposal, [0015](issues/0015.md).

## The problem

An agent that reacts to outside activity has to notice it. Polling costs a full
agent turn per tick whether or not anything happened, and it delays every event
by up to a whole interval. botwatch turns the poll into a block. One process
waits on every source at once, prints what happened, and exits. The caller acts
on that output and runs it again.

Three properties shape everything below:
- **One-shot.** There is no daemon. All memory between runs is a state file
  the caller holds.
- **Cheap to wait, prompt to wake.** An idle run costs one blocked process. A
  burst of events costs one wake, not one per event.
- **Nothing lost.** Ending a run, normally or not, never skips an event.

## Sources, updates and events

A **source** watches one thing, such as one Slack conversation's top level or
one Slack thread. In code, a source is an async generator of **updates**
(`botwatch.Source`):

```python
@dataclass(frozen=True)
class Update[StateT, EventT: Event]:
    events: tuple[EventT, ...]   # zero or more, in source order
    state: StateT                # the source's full state after these events
```

- **Creation.** A source is created from its previous state, or from none to
  start from now. Its first update is always a *checkpoint*: no events, just
  its starting state.
- **Lifetime.** It yields forever. Its consumer stops it by cancelling the pull
  in progress, or by closing it when none is, and its cleanup runs either way.
  It fails by raising.
- **State.** A source's state is a typed model that only the source
  interprets. Whoever consumes the updates handles each one whole: it writes
  the events, then keeps the state. The state always accounts for exactly the
  events written, so no separate step can get them out of step.

An **event** is a model with a `type` literal namespaced by its source type
(`slack.message_posted`), or by `botwatch` for events the run emits itself
(`botwatch.source_failed`). Every event has an `id`, unique within its source
and stable across replays, and a `time`. The set of event types is closed: the
output is validated against their union.

A source's **key** is derived from what it watches: `slack:C0123456789`, or
`slack:C0123456789/1785600000.000200` for a thread. A source can therefore
never inherit another source's state, and the state file needs no migration
when sources are added or removed.

## The run

`botwatch --config watch.yaml [--state state.yaml]`:

1. **Load.** It loads and validates the config, and the state file if given.
   Either failing exits 2 before anything starts. So does a state file in a
   directory botwatch can't write to.
2. **Start.** It creates every configured source from its stored state.
3. **Select.** It waits on all of them at once, using turbopipes' `aselect`,
   through `botwatch.select` (`_select.py`). Each update's events are written
   to stdout as one JSON line each, flushed immediately, and its state is kept.
4. **Depart.** `select` waits up to `quiet_timeout` for an update with events,
   then exactly `linger` more, counted from that update, and then closes every
   source.
   - Checkpoints and failures never start the linger.
   - If every source ends or fails first, the run still lasts the quiet
     timeout, so a loop around botwatch is paced rather than spinning.
5. **Save.** It writes the state file atomically, through a temporary file and
   a rename. The new state holds each configured source's state from its last
   update; entries for unconfigured sources are dropped.

**Failures.** A source that raises is reported as a `botwatch.source_failed`
event with a fresh id, and stays down for the rest of the run. It keeps the
state from its last update before the failure. Errors that are only transient
never get this far: the API client retries them.

**Delivery** is at-least-once. Because the state is written only after a run
ends normally, a crashed run's events are repeated by the next run.

## The Slack source

`slack.watch(client, conversation, state=…, thread_ts=…, poll_interval=…)`
(`slack/_watch.py`) watches a conversation's top level through
`conversations.history`, or one thread through `conversations.replies`.

- **State.** The state is one position, `after`: the `ts` of the newest message
  reported. A Slack `ts` is fixed-width, so string order is time order.
- **Starting.** A fresh source starts just after the newest message already
  there. That position comes from Slack rather than the local clock, so clock
  skew can't skip or repeat a message. An empty conversation starts at the
  beginning.
- **Polling.** Each poll fetches the messages after `after`, following
  pagination. A pure step, `_apply_poll`, turns them into events and the next
  state. A poll that finds nothing yields nothing.
- **The client** (`slack/_api.py`) parses Slack's responses into private
  models. It retries rate limiting (honouring `Retry-After`), server errors and
  network errors without limit, logging each retry to stderr. A Slack error
  response raises `SlackError`.
- **Events** are `slack.MessagePosted`, botwatch's own model: `conversation`,
  `ts`, `thread_ts`, `user`, `bot_id`, `username`, `subtype` and `text`.
  Slack's own response objects never leave the client.

The top level and threads are separate sources because Slack's history of a
conversation doesn't include thread replies.

## Stages

A stage wraps a source and is itself a source. `botwatch.exclude_events` drops
the events a predicate matches but keeps each update's state, so skipped events
still move the source along. An update whose events are all dropped becomes a
plain checkpoint. Slack's `exclude` config is built from it, with rules over
`user`, `bot_id`, `username` and a `text` regex. A rule matches when every field
set in it matches, and a message is skipped when any rule matches.

## Code map

| Module | What it holds |
| --- | --- |
| `_source.py` | `Event`, `Update`, `Source` |
| `_select.py` | `select`, and the private `_linger` timing combinator |
| `_exclude.py` | `exclude_events` |
| `_config.py` | The config models: durations, source keys, `SlackSourceConfig` |
| `_state.py` | `StateFile` and the atomic write |
| `_run.py` | `run_once`, the output `Record` and `SourceFailed` |
| `_cli.py` | The `botwatch` command |
| `slack/` | The Slack client, timestamps, the source and `ExcludeRule` |

Adding a source type means adding:
- a generator that yields updates;
- its state and event models, with event types added to the output union;
- a config model with a `key` and a `watch(client, state=…)` method.

## Design specs

Properties the code is held to for as long as botwatch exists. Each is checked
by a test, and a change to one is a deliberate decision.

### Where a run ends never changes what is emitted

For every source whose state has been saved, and given the same observations,
successive runs emit the same sequence of that source's events once repeats are
dropped by `(source, id)`, `botwatch.*` events aside. This holds however each
run ends, a crash included. Only a crash may repeat events, and none skips one.

A new source's state is first saved by a run that ends normally after the
source has delivered its first update. Until then, each run starts it from that
run's own now. The gap between runs observes nothing, so several changes made
inside it can surface as one, just as they would with a slower poll.

Checked by `test_run_once__where_a_run_ends_never_changes_what_is_emitted` in
`tests/test_run.py`.
