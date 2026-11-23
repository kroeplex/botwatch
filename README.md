# botwatch

**A one-shot `select` over many event sources, for agents.**

An agent that reacts to the outside world usually polls: it wakes on a timer,
checks for new chat messages, commits or review comments, and mostly finds
nothing. Each wake costs a full turn, and each event waits up to a whole
interval before the agent sees it.

`botwatch` replaces the poll with a block. One invocation:

1. reads a **config** naming the sources to watch,
2. reads an optional **state file** recording how far each source got last
   time,
3. blocks until something happens (or a timeout elapses),
4. writes the new **events** to stdout as JSON lines,
5. writes the advanced state back, and exits.

The caller runs it, reads the events, acts on them and runs it again. There's
no daemon, no hidden state, and no cursor-keeping in the agent itself.

## Status

**Early implementation.** Slack sources work as a Python library
(`botwatch.slack`); the `botwatch` command comes next. The design proposal is
[0015](docs/issues/0015.md).

## The shape of it

```console
$ botwatch --config watch.yaml --state state.yaml
{"source": "slack:C0123456789", "event": {"type": "slack.message_posted", "...": "..."}}
{"source": "slack:C0123456789/1785600000.000200", "event": {"type": "slack.message_posted", "...": "..."}}
```

A **source** is one thing being watched: a Slack conversation's top level or a
single Slack thread, and later pull requests, git repositories and message
queues.

Two properties do most of the work:

- **It's one-shot.** Everything botwatch remembers between runs is in the state
  file the caller hands it. There's no daemon to supervise and nothing else to
  fall out of sync.
- **It lingers.** The first event ends the long quiet wait at once. A short
  linger then lets whatever else is arriving join the same run. A burst costs
  one run, and the first event isn't delayed.
