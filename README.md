# botwatch

**Let your AI agent sleep until something actually happens.**

An agent that reacts to the outside world (Slack channels and threads today,
GitHub pull requests soon) usually polls: it wakes every few minutes, checks
every source, finds nothing, and goes back to sleep. For an LLM agent each wake
is a full turn over its whole context. Most of the tokens you pay for go on
re-reading that context only to confirm nothing happened, and every real event
still waits for the next tick.

botwatch does the waiting instead. It's one command: it watches all your
sources at once, exits the moment something happens, and prints the new events
as JSON lines. Your agent runs it, idles for free, and wakes only when there's
something to read.

There's no daemon, server or message broker to set up: just a config file, an
optional state file and a process that exits.

```console
$ cat watch.yaml
quiet_timeout: 55m
linger: 5s
sources:
  - type: slack
    conversation: C0123456789              # a channel's top level
  - type: slack
    conversation: C0123456789
    thread_ts: '1785600000.000200'         # one thread in it

$ SLACK_TOKEN=xoxb-… botwatch --config watch.yaml --state state.yaml
{"source":"slack:C0123456789/1785600000.000200","event":{"type":"slack.message_posted","id":"1785600123.000100","time":"2026-08-01T16:02:03.000Z","conversation":"C0123456789","ts":"1785600123.000100","thread_ts":"1785600000.000200","user":"U0456ABC","bot_id":null,"username":null,"subtype":null,"text":"can you take another look?"}}
```

That run blocked until someone replied in the thread, printed the reply, and
collected for five more seconds in case more arrived. Then it saved each
source's place in `state.yaml` and exited. Run it again and it carries on where
it left off.

## Is it for you?

**Yes, if:**
- you run an agent loop, such as a Claude Code session or your own harness,
  that should react to Slack conversations, and later GitHub and more;
- you want it idle and cheap between events, rather than polling on a timer;
- you'd rather not stand up infrastructure to make that happen.

**No, if** you need a high-throughput event pipeline, exactly-once delivery, or
a hosted service. botwatch is a small, one-shot command-line tool.

## How it works

- **It polls so your agent doesn't.** Each source calls Slack's Web API every
  `poll_interval` (30s by default), so an event surfaces within about that,
  plus `linger`. No event subscription or public endpoint is needed.
- **One run, one wait.** botwatch watches every configured source
  concurrently. It waits up to `quiet_timeout` for the first event. After that
  it keeps collecting for `linger`, so a burst of messages costs one wake
  rather than one each. Then it exits. Your loop decides whether to run it
  again.
- **The state file is its memory.** It records how far each source has got,
  and is written atomically on exit. Without one, every run starts from now.
- **At-least-once.** If a run is killed, the next one repeats what it had
  already printed rather than skipping anything. Dedupe on `(source, id)`.
- **Failures don't wake you early.** A source that breaks prints a
  `botwatch.source_failed` line and stays down for that run. A run with nothing
  to report always lasts the full quiet timeout, so a loop around botwatch never
  spins.

## Install

botwatch needs Python 3.14 and isn't on PyPI yet:

```console
$ pip install git+https://github.com/kroeplex/botwatch
```

For Slack, set `SLACK_TOKEN` to a bot or user token with the history scope for
each kind of conversation you watch: `channels:history`, `groups:history`,
`im:history` or `mpim:history`. A bot token reads only conversations the bot
has been added to.

## Using it from an agent

Run botwatch, hand its output to the agent, and repeat:

```bash
while true; do
  events="$(botwatch --config watch.yaml --state state.yaml)" || break
  [ -n "${events}" ] && my-agent --handle "${events}"
done
```

With Claude Code, run botwatch as a background command: the session is woken
when the command exits, and costs nothing while it waits.

- **Keep the next wake cheap.** If your model provider caches prompts for an
  hour, a `quiet_timeout` just under that (say `55m`) means the agent wakes at
  least that often and its context stays cached.
- **Don't wake yourself.** If the agent posts to the conversations it watches,
  skip its own messages with `exclude` (below). Otherwise every reply it sends
  wakes it again.

## Configuration

A config is YAML, and JSON works too, since JSON is YAML.

```yaml
quiet_timeout: 55m   # how long to wait for the first event (default 20m)
linger: 5s           # how much longer to collect after it (default 5s)
sources:
  - type: slack
    conversation: C0123456789      # channel, private channel, DM or group DM
    thread_ts: '1785600000.000200' # optional: watch just this thread
    poll_interval: 30s             # default 30s
    exclude:                       # optional: messages to skip
      - user: U0123BOT             # every field in a rule must match
        text: '^\[op 1\]'          # a regex over Slack's raw message text
```

Both IDs are in the link to a thread's first message:
`…/archives/C0123456789/p1785600000000200` is conversation `C0123456789`,
`thread_ts` `1785600000.000200`. Quote `thread_ts`, or YAML reads it as a
number.

Each source watches one thing: a conversation's top level, or one thread.
Slack's history of a conversation doesn't include thread replies, so watch each
thread you care about as its own source.

Sources on one conversation share a single history call each
`poll_interval`. A thread makes a call of its own only when it has a new reply,
or when its first message is older than the conversation's latest 200 messages.
Slack rate-limits each method to about 50 calls a minute per app, so with many
busy or old threads, raise `poll_interval` or prune the watch set.

A source's identity is derived from what it watches, such as
`slack:C0123456789/1785600000.000200`, so the state file needs no migration as
the watch set changes. A source you add starts from now. A source you remove
has its state dropped on the next run.

### Growing and pruning the watch set

Long-running agents keep joining new threads, and eventually stop caring about
old ones. For now that means editing the YAML.

> **Coming soon:** CLI commands to add, remove and list sources, and to inspect
> a config or state file, so neither agents nor humans have to hand-edit YAML.

## Output

Each event is one JSON line, flushed as it's written:

```json
{"source": "slack:C0123456789", "event": {"type": "slack.message_posted", "id": "…", "time": "…", …}}
```

| Field | Meaning |
| --- | --- |
| `source` | Which source it came from. |
| `event.type` | What happened: `slack.message_posted`, or `botwatch.source_failed`. |
| `event.id` | Unique within its source and stable across runs. Dedupe on `(source, id)`. |
| `event.time` | When it happened at the source. |

Each event type then carries its own fields: for Slack, `conversation`, `ts`,
`thread_ts`, `user`, `bot_id`, `username`, `subtype` and `text`; for
`botwatch.source_failed`, `error`, what the source raised. Diagnostics go
to stderr. The exit status is 0 after a normal run, and 2 for a bad config,
state file or environment.

## Using it from Python

The same pieces work as a library: Slack sources are async generators, and
`botwatch.select` waits on them the way the command does. See the
[Python API](docs/api.md).

## Learn more

- [Design](docs/design.md): how botwatch works and why.
- [CLI reference](docs/cli.md).
- [Python API](docs/api.md).
- [Issue tracker](docs/issues/README.md), including the
  [design proposal](docs/issues/0015.md) it was built from.

## Status

Early, but working: new messages in Slack conversations and threads. Edits,
deletions and reactions aren't reported yet. GitHub pull requests, retiring
sources that have gone quiet, and a CLI for managing the watch set are next.

## License

[Apache License 2.0](LICENSE).
