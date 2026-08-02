# CLAUDE.md — botwatch

## Kroeplex shared agent instructions

Conventions common to all Kroeplex repos live in **dev-docs**, imported at the
end of this file via the committed `.dev-docs` symlink (it chains up to a
one-time per-machine link beside the repo clones; run the `link-dev-docs` skill
if it ever fails to resolve).

Everything below is specific to this repo and wins over the common guide on
conflict.

## What this repo is

`botwatch` is a **one-shot `select` over many event sources**, meant to be
executed by an agent that needs to react to the outside world without doing its
own cursor bookkeeping. It loads a config of channels plus a state file, blocks
until events arrive or a timeout elapses, emits the new events on stdout,
advances the state, and exits. See the [README](README.md) for the shape of it
and the [issue tracker](docs/issues/README.md) for the design.

The repo is currently **design-stage**: no implementation yet.

## Repo-specific notes

- **This repo is intended to be open sourced.** Write everything — code,
  docs, issues, commit messages, PR descriptions — for a public audience from
  the start. Keep the design generic; `botwatch` is not a Kroeplex tool that
  happens to be shareable, it is a general-purpose tool whose first consumer
  happens to be Kroeplex.
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
  documents should stay **implementation-agnostic** — pseudo-code for
  illustration, not Python-specific contracts.
- **"Channel" is overloaded, and the ambiguity is dangerous here.** In this
  repo it means a stream of events in the concurrency sense — the thing you
  `select` over. A Slack channel is one kind of botwatch channel. Say "Slack
  channel" whenever you mean that.
- **Issue tracker:** [`docs/issues/`](docs/issues/); regenerate its index with
  `scripts/render_issues`.

## Common agent guide (auto-imported once `.dev-docs` is linked)

@.dev-docs/docs/agent-guide/README.md
