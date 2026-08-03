---
name: link-dev-docs
description: Repair the `.dev-docs` symlink chain that points a Kroeplex repo at the local dev-docs clone, so the shared agent guide and style guides load. Use when `.dev-docs/docs/agent-guide/README.md` doesn't resolve — usually because the one-time per-machine link beside the repo clones is missing. Not needed once it resolves.
---

<!--
  VENDORED FILE — source of truth in dev-docs:
    https://github.com/kroeplex/dev-docs/blob/main/boilerplate/.claude/skills/link-dev-docs/SKILL.md
  Copied verbatim into each Kroeplex repo; edit it upstream, not here.
-->

# link-dev-docs

Kroeplex repos reach the shared agent guide through a **`.dev-docs`** symlink
chain ending at a local clone of
[dev-docs](https://github.com/kroeplex/dev-docs). Every link in the chain is
committed and carries the same target, `../.dev-docs`, so each hop climbs one
directory and looks for the same name again — until it reaches the single
**per-machine** link beside the clones:

```
~/src/kroeplex/dev-docs/                            the clone
~/src/kroeplex/.dev-docs                -> dev-docs        ← the only uncommitted link
~/src/kroeplex/<repo>/.dev-docs                     -> ../.dev-docs  ┐
~/src/kroeplex/<repo>/.claude/.dev-docs             -> ../.dev-docs  │ committed
~/src/kroeplex/<repo>/.claude/worktrees/.dev-docs   -> ../.dev-docs  ┘
```

**So the usual repair is one link, once, for the whole machine** — not something
per repo.

## Two rules before any step

**1. Every block below is self-contained. Run them one at a time and don't
carry values between them.** Each block re-derives what it needs, because each
tool call gets a **fresh shell** — a variable set in one block is gone by the
next, and an empty path variable silently retargets `"${x}/.."` to the
filesystem root. The steps branch on their own output anyway, so they cannot be
run as a single command.

**2. Two different roots, deliberately.** They are not interchangeable:

- **`tree_root`** (`git rev-parse --show-toplevel`) — the tree you are *in*,
  worktree or main checkout. Used to ask **"does the guide load here?"**, which
  is the only question that justifies stopping.
- **`main_root`** (`dirname "$(git rev-parse --path-format=absolute --git-common-dir)"`)
  — always the main checkout, from anywhere. Used to **repair**, since every
  link lives there.

⚠️ **Never diagnose or repair from where you happen to be standing.** The three
repo links are committed, so a worktree has all of them — meaning from a
worktree root `../.dev-docs` is `<repo>/.claude/worktrees/.dev-docs`, a
*committed* link that resolves fine. Diagnose from there and every hop looks
healthy while the chain is plainly broken; "repair" from there and `ln -sf`
silently overwrites a **tracked** link with a machine-local path, leaving the
real break unfixed and staging something that breaks every other machine.

That same fact has a second consequence worth holding onto: **a worktree's chain
runs through the main checkout's working tree.** Its first hop lands at
`<repo>/.claude/worktrees/.dev-docs`, so the main checkout's *current branch*
decides whether any worktree can resolve the guide — and a branch there
predating these links breaks them all at once, with each worktree's own links
intact. Suspect that before suspecting the worktree.

That is the likely situation, not a corner case: the hook that sent you here
fires in whichever session hit the problem, and agents work from worktrees.

## 1. Does the guide load *here*?

```bash
tree_root=$(git rev-parse --show-toplevel)
[[ -e "${tree_root}/.dev-docs/docs/agent-guide/README.md" ]] && echo LINKED || echo BROKEN
```

`LINKED` → stop. Read `.dev-docs/docs/agent-guide/README.md` and get on with the
user's request.

**This must test the current tree, not the main checkout.** From the main
checkout the chain is two hops and touches neither `.claude/.dev-docs` nor
`.claude/worktrees/.dev-docs` — the two links a worktree depends on, and the two
most likely to be absent, since `.claude/worktrees/.dev-docs` is the one needing
`git add -f`. Test the wrong tree and a repo missing that link reports `LINKED`
here while the hook keeps reporting the break every session: a standoff that never
resolves.

## 2. Find the break

```bash
main_root=$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")
for p in .claude/worktrees/.dev-docs .claude/.dev-docs .dev-docs ../.dev-docs; do
  f="${main_root}/$p"
  if [[ ! -L "$f" && ! -e "$f" ]]; then
    t='(missing)'
  else
    t=$(readlink "$f") || t='(not a symlink)'
    tgt="$t"; [[ "$tgt" == /* ]] || tgt="$(dirname "$f")/$tgt"
    [[ -e "$tgt" || -L "$tgt" ]] || t="${t}   <- DANGLING"
  fi
  printf '%-34s %s\n' "$p" "$t"
done
```

**It reports whether each hop *resolves*, not merely whether a link exists.**
`readlink` prints the target string without following it, so a link whose target
has moved looks identical to a healthy one — and that case is common enough to
matter: move or rename the clone and `../.dev-docs` still prints `dev-docs`.

**`DANGLING` marks one hop, not the whole chain.** Testing a path with plain `-e`
would follow it all the way down, so a single break at the top marks *every* row
— including links that are perfectly correct, and rows that already say
`(missing)`. On a fresh machine, the most common broken state of all, that lights
up all four rows and implicates three links that are fine. Comparing each link's
own *immediate* target instead confines the marker to the **hop above the break**
rather than lighting the whole chain: on a fresh machine that is one row instead
of four, and the `(missing)` row directly beneath it names the actual break.
Testing that target with `-e || -L` is what makes a next hop that is itself
dangling still count as present.

**The loop runs in chain order**, deepest hop first, which is why the reading
works: the marked row is the hop that couldn't resolve, and **the row directly
beneath it** names what its target was. A `DANGLING` on a correct committed link
is expected when the break is below it — it is not a claim that the link is
wrong, so read the pair rather than the marker alone.

Don't reorder these into `ls` order or alphabetically. The adjacency is the whole
reading instruction, and it only holds while the list matches the chain. Note the
diagram at the top of this file draws that same chain **the other way up**, from
the clone down — that order explains the scheme, this one makes the diagnostic
readable. They disagree on purpose; the diagram is not a reason to reorder.

Three outcomes:

**`../.dev-docs` missing or dangling** — the common case, and the only one that
isn't a repo bug. It's the per-machine link. Go to §3.

**One or more of the three *committed* links missing at `main_root`** — two very
different causes, and you must tell them apart before touching anything:

```bash
main_root=$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")
git -C "${main_root}" rev-parse --abbrev-ref HEAD
git -C "${main_root}" cat-file -e HEAD:.claude/worktrees/.dev-docs 2>/dev/null \
  && echo 'HEAD carries the links' || echo 'HEAD predates the links'
git -C "${main_root}" for-each-ref --format='%(refname:short)' \
  | while read -r ref; do
      git -C "${main_root}" cat-file -e "${ref}:.claude/worktrees/.dev-docs" \
        2>/dev/null && { echo "carried by: ${ref}"; break; }
    done || echo 'carried by: no ref'
```

**Ask what HEAD's *tree* contains, not what the branch is called.** The branch
name tells you nothing about whether it carries the links, and `status
--porcelain` is silent in *both* causes — the links are absent-and-untracked
either way — so neither one separates them. `cat-file -e` asks the question the
two bullets below actually turn on. The second command answers "does the work
exist anywhere?", which is what distinguishes a parked checkout from a repo that
was never vendored; scanning refs rather than `origin/HEAD` matters because a
clone need not have `origin/HEAD` set, and its absence would otherwise read as
"never vendored".

`HEAD predates` + `carried by: <some ref>` is the first bullet. `HEAD predates` +
`carried by: no ref` is the second. The `|| echo` matters: without it the loop
ends on the last failed `cat-file` and the whole block exits 128 printing
nothing, so the never-vendored answer arrives looking like a broken command
rather than an answer.

- **The main checkout is simply on a branch that predates these links.** This is
  the common one, and *nothing is wrong with the repo*. Because a worktree's
  chain runs through `<repo>/.claude/worktrees/.dev-docs` — a file in the main
  checkout's tree — that branch choice breaks every worktree at once. 🚫 **Do not
  create or commit anything.** Say what you found and let whoever owns that tree
  switch it back; another session may be mid-task there, and in some repos the
  live checkout *is* what a running agent loads.
- **The repo really was vendored incompletely** — the branch there does carry the
  links, and they are still absent. Most often `.claude/worktrees/.dev-docs`,
  which needs `git add -f` (see the [boilerplate
  README](https://github.com/kroeplex/dev-docs/blob/main/boilerplate/README.md)).
  `mkdir -p` first: a repo that missed the force-add has no `.claude/worktrees/`
  at all in a fresh clone, since it's gitignored and holds one tracked file.

  ```bash
  main_root=$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")
  mkdir -p "$(dirname "${main_root}/<the missing path>")"
  ln -s ../.dev-docs "${main_root}/<the missing path>"
  ```

  🚫 **Create the link; don't stage or commit it.** `ln -s` unblocks the session,
  which is the urgent part. `git add` mutates the **index** of a tree that may
  belong to another session, quietly adding paths to whatever it commits next.
  Report the missing link and let it go through a normal PR.

**Everything at `main_root` healthy, but §1 said `BROKEN`** — then the break is in
*your* tree. Two causes, and they need different fixes, so check the cheap one
first:

**(a) The worktree is in the wrong place.** Every hop resolves against its own
*parent directory*, so a worktree reaches the chain only from directly inside
`<repo>/.claude/worktrees/`. Anywhere else on disk — or nested one level deeper —
fails **while carrying three perfectly correct links of its own**, which is why
the loops above all look healthy.

```bash
tree_root=$(git rev-parse --show-toplevel)
main_root=$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")
[[ "${tree_root}" == "${main_root}" \
   || "$(dirname "${tree_root}")" == "${main_root}/.claude/worktrees" ]] \
  && echo 'location OK' \
  || echo "WRONG LOCATION: ${tree_root}"
```

Fix by recreating the worktree at `<repo>/.claude/worktrees/<name>/`. Nothing to
repair — no link is wrong.

**(b) The branch predates the links**, so the checkout doesn't contain them yet.
Confirm by running the diagnostic loop against `tree_root` — presence only, with
no dangling check: `main_root` is healthy by hypothesis here, so nothing below it
can dangle and the extra detection would never fire:

```bash
tree_root=$(git rev-parse --show-toplevel)
for p in .dev-docs .claude/.dev-docs .claude/worktrees/.dev-docs; do
  t=$(readlink "${tree_root}/$p" 2>/dev/null) || t='(missing)'
  printf '%-34s %s\n' "$p" "$t"
done
```

Fix it by getting the links into that tree — rebase the branch onto one that has
them.

⚠️ **Check (a) before doing (b)'s rebase.** In case (a) this loop prints three
correct links, which reads as "the tree is fine" and leaves you with no next
step — or worse, sends you rebasing a branch that was never the problem.

🚫 **In neither case do you "repair" anything at `main_root`**: it is already
correct, and writing there turns a local problem into a real breakage.

## 3. Create the per-machine link

`"${main_root}/.."` is the directory holding the clones.

⚠️ **`-f` is correct here, and nowhere else in the chain.** Every command below
uses `ln -sfn`, because the link may already exist and *dangle* — that is the
state §2 routes here, and the state the hook deliberately declines to touch, so a
bare `ln -s` dead-ends on `File exists` at the one step meant to fix it. Forcing
is safe **only** at this hop: `<clones>/.dev-docs` is the single link in the chain
that isn't committed, so overwriting it changes nothing tracked and affects no
other machine. At the three per-repo hops, §2's ⚠️ stands — `-f` there silently
replaces a tracked link with a machine-local path.

`-n` is not decoration. Without it, `ln -sf` onto a link that already points at a
real directory *descends into it* and creates `<clones>/dev-docs/dev-docs`
instead of replacing the link.

- **A dev-docs clone is already there** (the standard layout):

  ```bash
  main_root=$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")
  [[ -e "${main_root}/../dev-docs/docs/agent-guide/README.md" ]] \
    && ln -sfn dev-docs "${main_root}/../.dev-docs" && echo DONE
  ```

- **It's somewhere else** → link it by absolute path:

  ```bash
  main_root=$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")
  ln -sfn /abs/path/to/dev-docs "${main_root}/../.dev-docs"
  ```

- **There's no clone at all** → **ask the user** before cloning; don't pick a
  location yourself. Then:

  ```bash
  main_root=$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")
  git clone <kroeplex/dev-docs remote> "${main_root}/../dev-docs"
  ln -sfn dev-docs "${main_root}/../.dev-docs"
  ```

  Use whatever GitHub access this repo uses for the `kroeplex` org (on Karl's
  machines that's the `kkroening` account — the `gh-kral` alias or the matching
  SSH remote).

## 4. Verify — here, and from a fresh worktree

The main checkout resolves even when the middle links are missing, so checking it
alone proves less than it looks like. Check the tree you're in *and* a throwaway
worktree:

```bash
tree_root=$(git rev-parse --show-toplevel)
main_root=$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")
head -1 "${tree_root}/.dev-docs/docs/agent-guide/README.md"
git -C "${main_root}" worktree remove --force .claude/worktrees/probe 2>/dev/null
git -C "${main_root}" branch -D dev-docs-probe 2>/dev/null
git -C "${main_root}" worktree add .claude/worktrees/probe -b dev-docs-probe
head -1 "${main_root}/.claude/worktrees/probe/.dev-docs/docs/agent-guide/README.md"
git -C "${main_root}" worktree remove --force .claude/worktrees/probe
git -C "${main_root}" branch -D dev-docs-probe
```

Both `head -1`s should print the guide's first line. If the worktree one fails
while the others work, a committed link is missing — that's §2, not §3.

The probe **cleans up before it starts, and unconditionally after**. A fixed name
would otherwise collide with whatever a previously-failed run stranded, and a
cleanup chained behind `&&` is skipped exactly when the removal failed. This step
only ever runs when something is already broken; it should not be able to add to
the mess.
