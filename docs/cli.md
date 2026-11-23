# CLI reference

## `botwatch`

```console
$ botwatch --config CONFIG [--state STATE]
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

**Exit status:**
- **0:** a normal run, whether or not anything happened.
- **2:** a bad config, state file or environment, reported on stderr before anything runs.
- **Anything else:** botwatch itself crashed. The state file is left as it was, so the next run repeats rather than skips.

**Output.** stdout carries only JSON lines, one event each. See
[Output](../README.md#output). Diagnostics and retries are logged to stderr.

## Coming soon

Commands for managing the watch set without hand-editing YAML: adding,
removing and listing sources, and inspecting a config or state file.
