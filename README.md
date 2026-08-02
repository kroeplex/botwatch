# botwatch

**A one-shot `select` over many event sources, for agents.**

An autonomous agent that needs to react to the outside world — new chat
messages, new commits, a pushed event — has to keep track of *what it has
already seen*, per source, across invocations. Doing that inside the agent is
expensive and error-prone: it burns tokens on bookkeeping, and the bookkeeping
is exactly the kind of fiddly state management that a language model gets
subtly wrong.

`botwatch` takes that job. One invocation:

1. reads a **config** naming the sources to watch,
2. reads a **state file** recording how far it got last time,
3. blocks until something happens (or a timeout elapses),
4. writes any new **events** to stdout,
5. writes the advanced state back, and exits.

The caller runs it, reads the events, and acts. No daemon, no hidden state, no
per-source cursors inside the agent's head.

## Status

**Early design.** The repository currently holds the design discussion; there
is no implementation yet. Start with the
[issue tracker](docs/issues/README.md).

## The shape of it

```console
$ botwatch --config watch.yaml --state state.yaml
{"channel": "team-chat", "kind": "message", "...": "..."}
{"channel": "upstream-main", "kind": "commit", "...": "..."}
```

Two properties do most of the work:

- **It is one-shot and stateless.** Everything `botwatch` remembers between
  runs is in the state file the caller hands it. Nothing is cached elsewhere,
  so there is no daemon to supervise, no lock to break, and no divergence
  between what the caller thinks was seen and what was seen.

- **It waits at the station.** A naive poller either returns the instant it
  finds one event (so a burst costs one invocation each) or waits a fixed
  window before returning (so the first event pays the whole window in
  latency). `botwatch` does neither: the first event ends the long quiet wait
  immediately, then a short boarding window lets whatever else is arriving come
  aboard. Bursts collapse into one invocation *and* the first event is not
  delayed.

"Channel" here is used in its concurrency sense — a stream of events you can
select over — not in the sense of a chat channel. A chat channel is one *kind*
of botwatch channel, alongside git repositories, pub/sub topics, and whatever
else grows a driver.

## Where it came from

`botwatch` is being built inside Kroeplex, a family Slack workspace and
knowledge graph, whose resident agent — Kroebot — is its first and most
demanding consumer. Kroebot polls; polling is what `botwatch` exists to
replace. Development happens in the open here even though Kroebot itself is
not open source, so expect that agent to show up throughout the commit history
as both author and dogfooder.
