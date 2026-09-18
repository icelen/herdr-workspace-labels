# herdr-workspace-labels

Persistent, arbitrary-key labels per [herdr](https://herdr.dev) workspace —
env, phase/status, or anything else you want to track — shown as sidebar
tokens and surviving herdr server restarts.

Built because `workspace.report_metadata` (the mechanism behind ad hoc
tagging scripts) is explicitly ephemeral: herdr resets it to empty on every
restore. This plugin owns its own persistence and republishes on startup, the
same way [bonkey/herdr-bookmark](https://github.com/bonkey/herdr-bookmark)
does for its fixed 3-mark bookmarks — generalized to arbitrary named labels.

## What you get

- `herdr-env <label>` / `herdr-env --clear` / `herdr-env` — tag a workspace
  with a test env (e.g. a PPE env name), survives restarts.
- `herdr-phase <label>` / `herdr-phase --clear` / `herdr-phase` — tag a
  workspace with a work phase/status (e.g. `developing`, `rollout`,
  `cleanup`, `archived - keep`), survives restarts.
- `herdr-label set <key> <value>` — the generic form behind both of the
  above; add your own label keys any time, no code changes needed.

Each label is published as its own `$<key>` sidebar token, on both the
workspace (Space rows) and every one of its panes (Agent rows), so it shows
up wherever you already reference `$env`/`$phase` in `config.toml`.

## Install

```bash
git clone https://github.com/icelen/herdr-workspace-labels ~/repos/herdr-workspace-labels
herdr plugin link ~/repos/herdr-workspace-labels/plugin
ln -s ~/repos/herdr-workspace-labels/bin/herdr-label ~/bin/herdr-label
ln -s ~/repos/herdr-workspace-labels/bin/herdr-env   ~/bin/herdr-env
ln -s ~/repos/herdr-workspace-labels/bin/herdr-phase ~/bin/herdr-phase
```

(Adjust `~/bin` to wherever is on your `PATH`; the wrappers just need to sit
next to each other, they resolve `plugin/label.py` relative to their own real
location so cloning to a different path on another machine works unchanged.)

Then render the tokens in herdr's `config.toml`:

```toml
[ui.sidebar.spaces]
rows = [
  ["state_icon", "workspace"],
  ["branch", "git_status"],
  [{ token = "$env", fg = "#89b4fa", bold = true }],
  [{ token = "$phase", fg = "#a6e3a1" }],
]

[ui.sidebar.agents]
rows = [
  ["state_icon", "workspace", "tab"],
  ["agent"],
  [{ token = "$env", fg = "#89b4fa", bold = true }],
  [{ token = "$phase", fg = "#a6e3a1" }],
]
```

```bash
herdr server reload-config
```

## How it works

- Labels are stored in `labels.json` under the plugin's state dir
  (`herdr plugin config-dir personal.workspace-labels`'s sibling state dir),
  keyed by the workspace's **checkout path** — so a label survives a herdr
  restart, a workspace rename, and even a new workspace id after reopening a
  worktree. A workspace with no checkout (rare) falls back to `id:<id>` and
  keeps its label only as long as that id lives.
- A `[[startup]]` hook republishes every saved label as a sidebar token when
  the herdr server starts, since the live token itself is wiped on restart.
- `workspace.created`/`focused` and `worktree.created`/`opened`/pane events
  republish a workspace's labels so a new pane or a reopened worktree picks
  them back up immediately.

## Maintenance

herdr doesn't know when a worktree is deleted, so a removed worktree's label
stays in `labels.json` as a harmless orphan (no sidebar row to show it, since
the workspace is gone). Run this occasionally to clean up:

```bash
herdr-label prune
```

This only drops entries keyed by a checkout path that no longer exists on
disk; entries keyed by `id:<workspace_id>` (no checkout was ever recorded)
are left alone.

## Multi-machine use

This is plain git + two symlinks, so setting it up on another machine (e.g. a
devbox) is the same three commands as above — nothing herdr-specific to sync,
`labels.json` is local per machine (which is correct: workspaces on different
machines are different workspaces).
