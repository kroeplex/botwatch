# Changelog

<!--
  Do NOT hand-write `### Changes` - the release tooling generates it from merged
  PR titles. Other subsections like `### Deployment notes` are hand-written. Anything
  written under `## [Unreleased]` will be included in the next release.
  See https://github.com/kroeplex/dev-docs/blob/main/docs/changelog-convention.md
-->

## [Unreleased]

## <span id="0-1-0" /> [0.1.0](https://github.com/kroeplex/botwatch/releases/tag/0.1.0) - 2026-11-23

The first release of botwatch: one command an AI agent runs instead of
polling. It waits on every source at once, exits when something happens, and
prints the new events as JSON lines, so the agent idles for free and wakes only
when there's something to read.

- **Sources:** Slack conversations and threads, and GitHub pull requests'
  comments and reviews.
- **Memory between runs:** a state file, written atomically, with which a run
  that ends early, or crashes, may repeat events but never skips one.
- **A watch set the agent manages itself:** `botwatch add`, `remove`,
  `defaults` and `show`, so it never has to edit the YAML.

See the [README](README.md) to get started.

### Changes

* Scaffold the repo: Kroeplex boilerplate and the issue tracker ([#1](https://github.com/kroeplex/botwatch/pull/1))
* Add the botwatch design RFC ([#2](https://github.com/kroeplex/botwatch/pull/2))
* Decompose the design RFC into eleven per-component implementation issues ([#3](https://github.com/kroeplex/botwatch/pull/3))
* Track the boilerplate resync this repo needs before it is published ([#4](https://github.com/kroeplex/botwatch/pull/4))
* Stand up the Python project: package, tooling, and CI ([#5](https://github.com/kroeplex/botwatch/pull/5))
* Replace the design RFC with a short proposal built on sources ([#15](https://github.com/kroeplex/botwatch/pull/15))
* Watch a Slack conversation or thread as an async generator ([#16](https://github.com/kroeplex/botwatch/pull/16))
* Select over many sources at once, departing on a quiet timeout and a linger ([#17](https://github.com/kroeplex/botwatch/pull/17))
* Add the botwatch command: config in, JSON lines out ([#18](https://github.com/kroeplex/botwatch/pull/18))
* Keep each source's place between runs in an optional state file ([#19](https://github.com/kroeplex/botwatch/pull/19))
* Skip messages that match caller-configured exclude rules ([#20](https://github.com/kroeplex/botwatch/pull/20))
* Resync the vendored boilerplate ahead of publishing ([#21](https://github.com/kroeplex/botwatch/pull/21))
* Depend on turbopipes 0.4.0 by its tag ([#24](https://github.com/kroeplex/botwatch/pull/24))
* Write the README and the design, CLI and API docs ([#23](https://github.com/kroeplex/botwatch/pull/23))
* Add scripts for cutting releases ([#25](https://github.com/kroeplex/botwatch/pull/25))
* Reserve Enhancement 0016 - Retire sources that have gone quiet, with a heads-up one run ahead
* Share one history call among a conversation's sources, so idle threads cost nothing ([#26](https://github.com/kroeplex/botwatch/pull/26))
* Add commands to edit and inspect the watch set ([#27](https://github.com/kroeplex/botwatch/pull/27))
* Watch GitHub pull requests' comments and reviews ([#29](https://github.com/kroeplex/botwatch/pull/29))
* License botwatch under Apache-2.0 ([#22](https://github.com/kroeplex/botwatch/pull/22))
* Keep the agents' git identity out of the repository ([#30](https://github.com/kroeplex/botwatch/pull/30))
* Set defaults for every source of a type ([#31](https://github.com/kroeplex/botwatch/pull/31))
* Let a new source start after a position the caller names ([#32](https://github.com/kroeplex/botwatch/pull/32))
