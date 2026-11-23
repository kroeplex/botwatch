# botwatch design

How botwatch works, for someone who has never seen it. This document
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

A **source** watches one thing, such as one Slack conversation's top level,
one Slack thread, or one GitHub pull request. In code, a source is an async
generator of **updates** (`botwatch.Source`):

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

## Editing the watch set

`botwatch add`, `botwatch defaults` and `botwatch remove` load the config,
change it (`Config.with_source`, `Config.without`, or a type's `defaults`), and
write it back atomically, keeping just the settings it gives, under a lock on
its directory. They never touch the state file: since keys derive from what a
source watches, an added source has no entry until a run saves one, and a
removed source's entry is dropped by the next run's save. `botwatch show`
reads both files and changes neither.

**Defaults.** A config's `defaults` hold settings for every source of a type.
`Config.resolve` applies them to a source as it runs: the default exclude rules
ahead of the source's own, and the default poll interval unless the source sets
one. They never change a source's key, and the config keeps them apart from
the sources, so editing one never rewrites the other.

## The Slack source

`slack.watch(client, conversation, state=…, thread_ts=…, poll_interval=…)`
(`slack/_watch.py`) watches a conversation's top level through
`conversations.history`, or one thread through `conversations.replies`. It
polls because only the Web API covers the gaps between runs: Slack keeps the
backlog, while the Events API and Socket Mode drop whatever arrives when no run
is listening.

- **State.** The state is one position, `after`: the source reports only
  messages after this `ts`. A Slack `ts` is fixed-width, so string order is
  time order.
- **Starting.** A fresh source starts just after the newest message already
  there. That position comes from Slack rather than the local clock, so clock
  skew can't skip or repeat a message. An empty conversation starts at the
  beginning.
- **Polling.** Each poll gets the messages after `after`, and a pure step,
  `_apply_poll`, turns them into events and the next state. A poll that finds
  nothing yields nothing.
- **Sharing a conversation.** Every source on one conversation reads the same
  `conversations.history` page, the conversation's newest 200 top-level
  messages. A caller gets any page fetched up to half its poll interval before
  it asked, so sources polling in step share one call, even a slow one.
  - **A thread** looks up its first message in that page. Slack marks it with
    `latest_reply`, and when that isn't after the thread's `after`, the thread
    has nothing new and makes no call. Otherwise, or when the first message is
    older than the page, it calls `conversations.replies`. A stale page can
    delay a reply by up to its age, but never skip one, since a thread only
    advances on its own `conversations.replies` result.
  - **The top level** reads the page directly whenever the page reaches back to
    `after`, and otherwise calls `conversations.history` from `after`,
    following pagination.

  Idle threads, the common case, therefore cost nothing beyond the shared
  call. All of this stays inside the client and `_watch.py`: each source is
  still its own generator.
- **The client** (`slack/_api.py`) parses Slack's responses into private
  models. It retries rate limiting (honouring `Retry-After`), server errors and
  network errors without limit, through `_utils/http.py`, logging each retry
  to stderr. A Slack error response raises `SlackError`.
- **Events** are `slack.MessagePosted`, botwatch's own model: `conversation`,
  `ts`, `thread_ts`, `user`, `bot_id`, `username`, `subtype` and `text`.
  Slack's own response objects never leave the client.

The top level and threads are separate sources because Slack's history of a
conversation doesn't include thread replies.

## The GitHub source

`github.watch(client, repo, pull, state=…, poll_interval=…)`
(`github/_watch.py`) watches one pull request through the REST API.

- **What it reports.**
  - `github.comment_posted` for each conversation comment.
  - `github.review_submitted` for each review once submitted, carrying the
    review's comments on the diff. Every diff comment belongs to a review, even
    a lone reply, so reviews cover them all.
  - `github.head_changed` when the head moves to another commit, with the
    SHAs before and after, and the new head commit's committer as `user` and
    message as `message`.
  - `github.state_changed` for each merge, close, reopen, and switch between
    draft and ready for review, from the pull request's issue events. The
    `closed` that comes with a merge is left out.
  - `github.checks_concluded` when the head's checks reach a conclusion other
    than the last one reported or recorded for that head: `failure` as soon as
    a check run or commit status fails, and otherwise, once all have finished,
    `cancelled` or `success`. Only each check's newest run counts, as a
    workflow can run again for the same commit. A check suite not yet
    completed holds back a success, since jobs that wait on others, or a
    workflow waiting on its concurrency group, have no check run yet; a queued
    suite with no runs counts only for its first minute, as some apps create
    suites that never run. A completed suite with no runs counts as failed if
    it failed to start, and as running while it awaits approval. A re-run that
    turns a failure into a success is reported too.
- **State.** The state is a time, `after`, by GitHub's clock, plus `seen`, the
  IDs of the events from then on already reported, and `head`, the head commit
  when last polled, with `head_changes`, a count that numbers each head change
  so no ID repeats, and `checks`, the last checks conclusion and its head, with
  its own count. A review is placed by when it was submitted rather than by
  its ID, which it takes while still pending. A push has no time of its own to
  place it by, so it's found by comparing the head with `head`, which a source
  without one records as it starts, and timed by the poll that found it. A
  source without `checks` records the conclusion as it starts too, unless it
  was reached at or after `after`, such as since its `start_after`: that
  conclusion is left for the first poll to report. A failure is reached when
  its first failing check finished; any other conclusion, when the last check
  did.
- **Settling.** GitHub's timestamps don't follow the order in which events
  become visible, and a poll lists events in several calls. So `after` trails
  the newest event reported, by up to ten seconds before the poll began, and
  `seen` keeps the next poll from reporting again what it lists twice.
- **Starting.** A fresh source starts from now by GitHub's clock: the `Date`
  of a call that doesn't count against the rate limit. Each poll starts the
  same way.
- **Polling.** Each poll reads the head's SHA, whose `ETag` changes only when
  the head does, and lists the comments since `after`, every review and the
  issue events, and the head's check runs, check suites and statuses. When a
  review is new, it lists every diff comment, grouped into its review, and
  when the head has moved, it reads the new head commit. A pure
  step, `_apply_poll`, keeps the events not yet reported, oldest first, and
  advances the state.
- **The client** (`github/_api.py`) counts a list's pages, since a 304 carries
  no `Link` header, and keeps each page's `ETag` to ask again conditionally.
  An unchanged poll is then answered with 304s, which GitHub doesn't count
  against the rate limit. It retries rate limiting (honouring `Retry-After`,
  the rate limit's reset, or a minute for a secondary limit), server errors and
  network errors without limit, and follows redirects; any other error raises
  `GitHubError`.

## Stages

A stage wraps a source and is itself a source. `botwatch.exclude_events` drops
the events a predicate matches but keeps each update's state, so skipped events
still move the source along. An update whose events are all dropped becomes a
plain checkpoint. Each source type's `exclude` config is built from it: Slack's
rules are over `user`, `bot_id`, `username` and a `text` regex, and GitHub's
over `user` and a `text` regex. A rule matches when every field set in it
matches, and an event is skipped when any rule matches.

## Code map

| Module | What it holds |
| --- | --- |
| `_source.py` | `Event`, `Update`, `Source` |
| `_select.py` | `select`, and the private `_linger` timing combinator |
| `_exclude.py` | `exclude_events` |
| `_config.py` | The config models: durations, source keys, each type's source config and state union, per-type `Defaults`, `Clients`, and the edits the commands make |
| `_state.py` | `StateFile` |
| `_run.py` | `run_once`, the output `Record` and `SourceFailed` |
| `_cli.py` | The `botwatch` command: `run`, `add`, `defaults`, `remove` and `show` |
| `_utils/` | Datetime formatting, the atomic file write, and retrying HTTP calls |
| `slack/` | The Slack client, timestamps, the source and `ExcludeRule` |
| `github/` | The GitHub client, the source and `ExcludeRule` |

Adding a source type means adding:
- a generator that yields updates;
- its state model, with a `type` literal, to `SourceState` (`_config.py`);
- its event models, to the output union (`_run.py`);
- a config model with a `key`, a `start_after` and its `start_state`, and a
  `watch(clients, state=…)` method, to `SourceConfig`;
- its field in `Defaults`;
- its client, to `Clients`, created in `_cli.py` with its token;
- its arguments to `botwatch add`, and a `describe()` on its state for
  `botwatch show`.

## Design specs

Properties the code is held to for as long as botwatch exists. Each is checked
by a test, and a change to one is a deliberate decision.

### Where a run ends never changes what is emitted

For every source whose state has been saved, or that has a `start_after`, and
given the same activity at that source, the events successive runs emit for it
form the same sequence however each run ends, a crash included, once repeats
are dropped by `(source, id)` and `botwatch.*` events are ignored. Only a run
that ends without saving its state, such as a crash, may cause repeats, and no
event is ever skipped. A GitHub source is held to the same set of events but
not to one order, since GitHub can reveal events out of time order, and its
whole-second times don't order the events within one second. Until a state
with its `head` is first saved, it measures pushes from the head at each run's
start.

Without either, a new source's state is first saved by a run that ends
normally after the source has delivered its first update. Until then, each run
starts it from that run's own now. The gap between runs observes nothing, so
several changes made inside it can surface as one, just as they would with a
slower poll.

Checked by `test_run_once__where_a_run_ends_never_changes_what_is_emitted` in
`tests/test_run.py`.
