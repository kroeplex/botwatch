# Issues

The in-repo issue tracker: one Markdown file per issue, numbered `NNNN.md`.
Conventions and how to write a good issue: **[the shared issue tracker guide](https://github.com/kroeplex/dev-docs/blob/main/docs/issue-tracking.md)**.

## Index

| # | Title | Kind | Status |
|---|---|---|---|
| [0016](0016.md) | (reserved) Retire sources that have gone quiet, with a heads-up one run ahead | Enhancement | Open |
| [0015](0015.md) | Watch many sources in one blocking run, each carrying its own state | Enhancement | Resolved |
| [0013](0013.md) | Resync the vendored boilerplate before this repo is published | Bug | In progress |
| [0012](0012.md) | Add the Pub/Sub driver, making `botwatch` push-capable | Enhancement | Superseded by 0015 |
| [0011](0011.md) | Add the GitHub driver for pull requests, reviews, and check runs | Enhancement | Superseded by 0015 |
| [0010](0010.md) | Add the git driver, watching refs in local and remote repositories | Enhancement | Superseded by 0015 |
| [0009](0009.md) | Add the Slack driver, one botwatch channel per conversation | Enhancement | Superseded by 0015 |
| [0008](0008.md) | Specify `prune` and bound retired-channel growth | Enhancement | Superseded by 0015 |
| [0007](0007.md) | Add `botwatch inspect` for reading a state file's health out of band | Enhancement | Superseded by 0015 |
| [0006](0006.md) | Wait on every channel at once: the select, the two clocks, and the departure guards | Enhancement | Superseded by 0015 |
| [0005](0005.md) | Define the channel driver contract and ship a conformance kit every driver must pass | Enhancement | Superseded by 0015 |
| [0004](0004.md) | Emit events as JSONL with a stable envelope, and report channel failures on the same stream | Enhancement | Superseded by 0015 |
| [0003](0003.md) | Persist run state atomically, with a channel lifecycle that survives reconfiguration | Enhancement | Superseded by 0015 |
| [0002](0002.md) | Load the config and enforce the channel key contract | Enhancement | Superseded by 0015 |
| [0001](0001.md) | botwatch: a one-shot `select` over many event sources | RFC | Superseded by 0015 |
