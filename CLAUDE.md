# CLAUDE.md — botwatch

## Kroeplex shared agent instructions

Conventions common to all Kroeplex repos live in **dev-docs**, imported at the
end of this file via the committed `.dev-docs` symlink, which chains up to a
one-time per-machine link beside the repo clones.

Everything below is specific to this repo and wins over the common guide on
conflict.

## What this repo is

`botwatch` is a **one-shot `select` over many event sources**, meant to be
executed by an agent that needs to react to the outside world without doing its
own cursor bookkeeping. It loads a config of sources plus an optional state
file, blocks until events arrive or a timeout elapses, emits the new events on
stdout, advances the state, and exits. See the [README](README.md) for the shape
of it and the design proposal, [0015](docs/issues/0015.md).

Implementation is under way, following 0015's rollout.

## Repo-specific notes

- **This repo is intended to be open sourced.** Write everything — code,
  docs, issues, commit messages, PR descriptions — for a public audience from
  the start. Keep the design generic; `botwatch` is not a Kroeplex tool that
  happens to be shareable, it is a general-purpose tool whose first consumer
  happens to be Kroeplex. The one exception is the **traceability header** an
  agent puts at the top of a PR description (an operator id and links back to
  the conversation the work came from) — that is machinery for finding things
  again, it is exempt from this rule, and it should not be stripped. Everything
  below it is the part written for a public reader.
- **Don't put private material in the history.** Kroeplex, Kroebot, and the
  fact that Kroebot dogfoods `botwatch` are all fine to name — they show up in
  the commit history regardless. Individual family members are not, except
  where they participate here directly: crediting a contributor for a point
  they raised in a PR, a commit, or the announcement channel is exactly right;
  narrating anything else about them is not. This is a best-effort habit, not
  a tripwire — write naturally and keep it in the back of your mind.
- **Language: Python**, chosen for the paved path across Kroeplex rather than
  for performance. Startup latency is dwarfed by the time spent waiting on the
  sources being watched, and a port to a compiled language stays open. Design
  documents may name the Python constructs the design rests on, such as async
  generators and turbopipes. Beyond those, they describe behaviour; any code is
  a labelled sketch.
- **A *source* is one thing being watched**: a Slack conversation's top level,
  or one Slack thread. botwatch only reads from it. Don't use "channel" for a
  source; say "Slack channel" when you mean one.
- **Issue tracker:** [`docs/issues/`](docs/issues/); regenerate its index with
  `scripts/render_issues`.

## Common agent guide (auto-imported once `.dev-docs` is linked)

@.dev-docs/docs/agent-guide/README.md
