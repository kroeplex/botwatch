# CLI reference

```console
$ botwatch [run] --config CONFIG [--state STATE]
$ botwatch add --config CONFIG slack CONVERSATION [--thread TS] [--poll-interval DURATION] [--exclude RULE]…
$ botwatch remove --config CONFIG KEY…
$ botwatch show --config CONFIG [--state STATE] [--json]
```

`run` is the default command, so `botwatch --config CONFIG` runs. Every command
takes `-h` for its own help.

**Exit status**, for every command:
- **0:** success. For `run`, a normal run, whether or not anything happened.
- **2:** a bad command line, config, state file or source, or a file that
  couldn't be written, reported on stderr.
- **Anything else:** botwatch itself crashed. No file it was writing is left
  half-written.

## `run`

```console
$ botwatch [run] --config CONFIG [--state STATE]
```

Waits until something happens at a configured source, then writes the events to
stdout as JSON lines, saves the state, and exits.

| Option | Meaning |
| --- | --- |
| `--config CONFIG` | A YAML (or JSON) file naming the sources to watch. See [Configuration](../README.md#configuration). |
| `--state STATE` | Where to keep how far each source got between runs. It's created if missing. Without it, every run starts from now. |

| Environment | Meaning |
| --- | --- |
| `SLACK_TOKEN` | The Slack token Slack sources read with. Required when any are configured. |

The config is read once, as the run starts, so a source added during a run is
first watched by the next one, from that run's start.

A bad config or state file, no `SLACK_TOKEN`, or a state file in a directory
botwatch can't write to exits 2 before the run. A state file that can't be
saved after it also exits 2, and the next run repeats this one's events. If
botwatch crashes, the state file is left as it was, so the next run repeats
rather than skips.

**Output.** stdout carries only JSON lines, one event each. See
[Output](../README.md#output). Diagnostics and retries are logged to stderr.

## `add`

```console
$ botwatch add --config CONFIG slack CONVERSATION [--thread TS] [--poll-interval DURATION] [--exclude RULE]…
added slack:C0123456789/1785600000.000200
```

Watches a Slack conversation's top level, or with `--thread` one thread in it.
Prints `added`, `updated` or `unchanged`, and the source's key.

| Option | Meaning |
| --- | --- |
| `CONVERSATION` | The conversation ID, such as `C0123456789`. |
| `--thread TS` | The `ts` of the thread's first message, such as `1785600000.000200`. |
| `--poll-interval DURATION` | How often to poll, such as `15s`. Default `30s`. |
| `--exclude RULE` | Messages to skip, as a YAML mapping, such as `'{user: U0123BOT}'` or `"{text: '^\[op 1\]'}"`. Repeat it for more rules. See [Configuration](../README.md#configuration). |

A source that's already watched gets exactly the settings given, so repeat any
you want to keep. Its key is unchanged, so it keeps its place in the state file.
The config is created if it doesn't exist.

## `remove`

```console
$ botwatch remove --config CONFIG KEY…
removed slack:C0123456789/1785600000.000200
```

Stops watching the sources with these keys, as `add` and `show` print them.
Prints `removed` and each key removed; a key that isn't watched is reported on
stderr and isn't an error. The next run drops their entries from the state file.

`add` and `remove` rewrite the config, atomically, with every setting at its
default left out, so comments and formatting in it are lost. A `.json` config
stays JSON.

## `show`

```console
$ botwatch show --config CONFIG [--state STATE] [--json]
quiet_timeout 55m, linger 5s
  slack:C0123456789                    every 30s  after 2026-08-01 16:02:03 UTC
  slack:C0123456789/1785600000.000200  every 15s  starts from now on its next run
In state.yaml but no longer configured, dropped on the next run:
  slack:C0OLD
```

Validates the config, and the state file if given, and lists each source with
its poll interval and, given the state, how far it has got. It also lists state
entries for sources no longer configured.

With `--json` it prints one JSON object instead:

```json
{
  "quiet_timeout": "55m",
  "linger": "5s",
  "sources": [
    {
      "key": "slack:C0123456789",
      "config": {"type": "slack", "conversation": "C0123456789", "thread_ts": null, "poll_interval": "30s", "exclude": []},
      "state": {"type": "slack", "after": "1785600123.000100"}
    }
  ],
  "unconfigured_state": ["slack:C0OLD"]
}
```

`state` is `null` for a source with no saved state, or when no `--state` is
given.
