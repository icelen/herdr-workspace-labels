#!/usr/bin/env python3
# Persistent, arbitrary-key labels per herdr workspace (env, phase, whatever you
# like), published as `$<key>` sidebar tokens on the workspace (Space rows) and
# on each of its panes (Agent rows).
#
#   python3 label.py set <key> <value>   set/replace one label on this workspace
#   python3 label.py clear <key>         drop one label from this workspace
#   python3 label.py clear-all           drop every label from this workspace
#   python3 label.py get [key]           print one label, or all labels, no herdr calls
#   python3 label.py list                print every workspace's saved labels
#   python3 label.py prune               drop entries whose checkout path is gone
#   python3 label.py sort [--dry-run]    sort each repo group's worktrees by label
#   python3 label.py --republish         (event hook) re-publish this workspace's labels
#   python3 label.py --publish-all       (startup hook) re-publish every workspace
#
# Add --sort to either hook to also re-sort afterwards. Setting or clearing
# the sort label re-sorts on its own.
#
# A sidebar token lives only in the running server, while a label is the
# user's own state, so labels are kept in labels.json under the plugin state
# dir and the [[startup]] hook republishes every workspace when herdr starts.
#
# A workspace is keyed by its checkout path, which survives a restart, a
# rename and a new workspace id (e.g. reopening a worktree); a workspace
# without a checkout falls back to `id:<id>` and keeps its labels only as
# long as that id lives.

import json
import os
import sys

SOURCE = "personal.workspace-labels"
STATE_FILE = "labels.json"
STATE_VERSION = 1
HERDR_TIMEOUT = 10

SORT_CONFIG_FILE = "sort.toml"
DEFAULT_SORT_KEY = "phase"
DEFAULT_SORT_ORDER = ["admin", "plan", "coding", "review", "testing", "rollout", "cleanup", "archived"]

_MISSING = object()


def log(message):
    print("label: %s" % message, file=sys.stderr, flush=True)


def env(name, default=""):
    return os.environ.get(name) or default


def herdr_bin():
    return env("HERDR_BIN_PATH", "herdr")


_STATE_DIR = None
_CONFIG_DIR = None


def config_dir():
    """HERDR_PLUGIN_CONFIG_DIR when herdr runs the command, else what
    `herdr plugin config-dir` prints (which also creates it). "" when neither
    is available."""
    global _CONFIG_DIR
    if _CONFIG_DIR is None:
        _CONFIG_DIR = env("HERDR_PLUGIN_CONFIG_DIR")
        if not _CONFIG_DIR:
            ok, out = run_herdr(["plugin", "config-dir", SOURCE])
            _CONFIG_DIR = out.strip() if ok else ""
    return _CONFIG_DIR


def state_dir():
    """HERDR_PLUGIN_STATE_DIR only exists when herdr itself runs a manifest
    command (an action, event hook, or startup hook). herdr-label is meant to
    be run directly from a shell, which never sets it -- so derive the same
    path herdr would use, from `herdr plugin config-dir`, which shares the
    plugin id path-mangling and the app dir name (`herdr` vs `herdr-dev` for
    a debug build) with the state dir, just under .config instead of
    .local/state. That keeps this in step with whichever `herdr` binary is
    actually on PATH, without hardcoding either piece."""
    global _STATE_DIR
    if _STATE_DIR is not None:
        return _STATE_DIR

    direct = env("HERDR_PLUGIN_STATE_DIR")
    if direct:
        _STATE_DIR = direct
        return _STATE_DIR

    fallback = os.path.join(env("TMPDIR", "/tmp"), "herdr-workspace-labels")
    config = config_dir()
    if not config:
        _STATE_DIR = fallback
        return _STATE_DIR

    # config is "<config_root>/plugins/config/<id_component>".
    id_component = os.path.basename(config)
    plugins_config = os.path.dirname(config)
    plugins = os.path.dirname(plugins_config)
    config_root = os.path.dirname(plugins)
    if os.path.basename(plugins_config) != "config" or os.path.basename(plugins) != "plugins" or not config_root:
        _STATE_DIR = fallback
        return _STATE_DIR
    app_dir_name = os.path.basename(config_root)

    xdg_state = env("XDG_STATE_HOME")
    if xdg_state:
        state_root = os.path.join(xdg_state, app_dir_name)
    else:
        home = env("HOME")
        if not home:
            _STATE_DIR = fallback
            return _STATE_DIR
        state_root = os.path.join(home, ".local", "state", app_dir_name)

    _STATE_DIR = os.path.join(state_root, "plugins", id_component)
    return _STATE_DIR


# --------------------------------------------------------------------------
# Running herdr
# --------------------------------------------------------------------------


def run_herdr(args, capture=True):
    """Return (ok, stdout). `ok` is False when herdr failed, timed out or is missing."""
    import subprocess

    try:
        done = subprocess.run(
            [herdr_bin()] + args,
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=HERDR_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return False, ""
    out = done.stdout.decode("utf-8", "replace") if capture and done.stdout else ""
    return done.returncode == 0, out


def socket_path():
    """HERDR_SOCKET_PATH when herdr exported it, else the socket the `herdr`
    on PATH reports, so a plain shell outside a pane still reaches it."""
    path = env("HERDR_SOCKET_PATH")
    if path:
        return path
    _, out = run_herdr(["status", "server"])
    for line in out.splitlines():
        name, _, value = line.partition(":")
        if name.strip() == "socket" and value.strip():
            return value.strip()
    return ""


def socket_request(method, params):
    """One raw socket API request, for methods the CLI doesn't wrap. Returns
    the response's result, or None after logging an error."""
    import socket

    path = socket_path()
    if not path:
        log("%s: no herdr socket found" % method)
        return None
    request = json.dumps({"id": SOURCE, "method": method, "params": params}) + "\n"
    received = b""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(HERDR_TIMEOUT)
            connection.connect(path)
            connection.sendall(request.encode("utf-8"))
            while b"\n" not in received:
                chunk = connection.recv(65536)
                if not chunk:
                    break
                received += chunk
    except OSError as error:
        log("%s: %s" % (method, error))
        return None
    try:
        response = json.loads(received.split(b"\n", 1)[0].decode("utf-8", "replace"))
    except ValueError:
        log("%s: unreadable response" % method)
        return None
    error = response.get("error") if isinstance(response, dict) else None
    if error:
        log("%s: %s" % (method, error.get("message") if isinstance(error, dict) else error))
        return None
    return response.get("result") if isinstance(response, dict) else None


# --------------------------------------------------------------------------
# JSON helpers (herdr's CLI JSON is one document, occasionally line-delimited)
# --------------------------------------------------------------------------


def load_json_docs(text):
    try:
        return [json.loads(text)]
    except ValueError:
        pass
    docs = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            docs.append(json.loads(line))
        except ValueError:
            continue
    return docs


def find_value(node, key):
    if isinstance(node, dict):
        for name, value in node.items():
            if name == key:
                return value
            found = find_value(value, key)
            if found is not _MISSING:
                return found
    elif isinstance(node, list):
        for value in node:
            found = find_value(value, key)
            if found is not _MISSING:
                return found
    return _MISSING


def iter_objects(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            for found in iter_objects(value):
                yield found
    elif isinstance(node, list):
        for value in node:
            for found in iter_objects(value):
                yield found


def json_str(text, key):
    for doc in load_json_docs(text):
        found = find_value(doc, key)
        if found is not _MISSING:
            return found if isinstance(found, str) else ""
    return ""


# --------------------------------------------------------------------------
# Workspaces
# --------------------------------------------------------------------------


def checkout_path(workspace):
    worktree = workspace.get("worktree")
    if not isinstance(worktree, dict):
        return ""
    value = worktree.get("checkout_path")
    return value if isinstance(value, str) else ""


def key_for(workspace_id, checkout):
    """What a workspace's labels are stored under. The checkout path survives a
    restart, a rename and a new workspace id; a workspace without a checkout
    keeps its labels only as long as its id lives."""
    return checkout or ("id:" + workspace_id)


def workspace_records():
    _, out = run_herdr(["workspace", "list"])
    found = []
    for doc in load_json_docs(out):
        for obj in iter_objects(doc):
            value = obj.get("workspace_id")
            if isinstance(value, str) and value:
                found.append(obj)
    return found


def workspaces():
    return [(record["workspace_id"], checkout_path(record)) for record in workspace_records()]


def checkout_of(workspace_id):
    for found, checkout in workspaces():
        if found == workspace_id:
            return checkout
    return ""


def panes_of(workspace_id):
    _, out = run_herdr(["pane", "list", "--workspace", workspace_id])
    found = []
    for doc in load_json_docs(out):
        for obj in iter_objects(doc):
            value = obj.get("pane_id")
            if isinstance(value, str) and value:
                found.append(value)
    return found


# --------------------------------------------------------------------------
# Stored labels
# --------------------------------------------------------------------------


def state_path():
    return os.path.join(state_dir(), STATE_FILE)


def load_state():
    """The saved labels, as {key: {label_name: value}}. An unreadable or
    malformed file reads as empty rather than losing the run; the next write
    replaces it."""
    try:
        with open(state_path(), "r", encoding="utf-8", errors="replace") as handle:
            document = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(document, dict):
        log("%s: not an object, ignoring it" % state_path())
        return {}
    labels = document.get("labels")
    if not isinstance(labels, dict):
        return {}
    found = {}
    for key, value in labels.items():
        if isinstance(key, str) and isinstance(value, dict):
            cleaned = {k: v for k, v in value.items() if isinstance(k, str) and isinstance(v, str) and v}
            if cleaned:
                found[key] = cleaned
    return found


def save_state(state):
    """Replace the state file. Written next to it and renamed, so an
    interrupted write leaves the previous labels in place."""
    path = state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    document = {
        "version": STATE_VERSION,
        "labels": {key: value for key, value in sorted(state.items()) if value},
    }
    temporary = path + ".new"
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    except OSError as error:
        log("cannot write %s: %s" % (path, error))
        try:
            os.remove(temporary)
        except OSError:
            pass
        return False
    return True


# --------------------------------------------------------------------------
# Publishing
# --------------------------------------------------------------------------


def report(kind, ident, token, value):
    """report-metadata <workspace|pane> <id>, clearing the token for an empty
    value."""
    args = [kind, "report-metadata", ident, "--source", SOURCE]
    args += ["--token", token + "=" + value] if value else ["--clear-token", token]
    ok, _ = run_herdr(args, capture=False)
    return ok


def publish_token(workspace_id, token, value):
    """Publish one token on the workspace (Space rows) and on each of its
    panes (Agent rows)."""
    ok = report("workspace", workspace_id, token, value)
    if not ok:
        log("workspace %s: report-metadata %s failed" % (workspace_id, token))
    for pane_id in panes_of(workspace_id):
        if not report("pane", pane_id, token, value):
            log("pane %s: report-metadata %s failed" % (pane_id, token))
            ok = False
    return ok


def republish_workspace(workspace_id):
    """Publish every saved label of one workspace. Event hooks run this, so a
    new or refocused pane gets the tokens its workspace already carries."""
    checkout = checkout_of(workspace_id)
    labels = load_state().get(key_for(workspace_id, checkout), {})
    ok = True
    for token, value in labels.items():
        if not publish_token(workspace_id, token, value):
            ok = False
    return ok


def publish_all():
    """Every workspace herdr currently holds, from the saved labels. The
    [[startup]] hook runs this: a token lives only in the running server, so
    without it a Space/Agent row stays empty until its workspace is next
    focused."""
    found = workspaces()
    if not found:
        log("workspace list is empty; reported nothing")
        return False
    ok = True
    for workspace_id, _checkout in found:
        if not republish_workspace(workspace_id):
            ok = False
    return ok


# --------------------------------------------------------------------------
# Sorting
# --------------------------------------------------------------------------


def load_sort_config():
    """(key, order) from sort.toml in the plugin config dir, falling back to
    DEFAULT_SORT_KEY/DEFAULT_SORT_ORDER. An empty key turns sorting off."""
    key, order = DEFAULT_SORT_KEY, DEFAULT_SORT_ORDER
    directory = config_dir()
    path = os.path.join(directory, SORT_CONFIG_FILE) if directory else ""
    if not path or not os.path.exists(path):
        return key, order
    try:
        import tomllib
    except ImportError:
        log("%s: needs python 3.11+ to read, using the defaults" % path)
        return key, order
    try:
        with open(path, "rb") as handle:
            document = tomllib.load(handle)
    except (OSError, ValueError) as error:
        log("%s: %s, using the defaults" % (path, error))
        return key, order
    if isinstance(document.get("key"), str):
        key = document["key"]
    if isinstance(document.get("order"), list):
        order = [value for value in document["order"] if isinstance(value, str) and value]
    return key, order


def sort_rank(value, order):
    """Where a label value sorts: its first matching entry in `order`, then
    values not in `order` alphabetically, then no value at all. An entry
    matches the whole value or its leading word, so `archived` also covers
    `archived - keep`."""
    if not value:
        return (2, "")
    lowered = value.lower()
    for index, entry in enumerate(order):
        entry = entry.lower()
        if lowered == entry or (lowered.startswith(entry) and not lowered[len(entry)].isalnum()):
            return (0, index)
    return (1, lowered)


def sorted_group(members, labels_of, key, order):
    """A repo group's ids in their sorted order: the group's main checkouts
    (as the sidebar always lists them first) and then its linked worktrees by
    label, keeping the current order between equal labels."""
    parents = [m for m in members if not m["worktree"].get("is_linked_worktree")]
    children = [m for m in members if m["worktree"].get("is_linked_worktree")]
    children.sort(key=lambda m: sort_rank(labels_of(m).get(key, ""), order))
    return [m["workspace_id"] for m in parents + children]


def sort_workspaces(dry_run=False):
    """Sort the linked worktrees inside each repo group by the sort label.
    Groups stay where they are and workspaces outside a group are never
    moved. A group the sidebar doesn't show as one (no main checkout open)
    is left alone too."""
    key, order = load_sort_config()
    if not key:
        return True
    records = [r for r in workspace_records() if isinstance(r.get("number"), int)]
    records.sort(key=lambda r: r["number"])
    state = load_state()

    def labels_of(record):
        return state.get(key_for(record["workspace_id"], checkout_path(record)), {})

    groups = {}
    for record in records:
        worktree = record.get("worktree")
        if isinstance(worktree, dict) and worktree.get("repo_key"):
            groups.setdefault(worktree["repo_key"], []).append(record)

    ok = True
    for repo_key, members in groups.items():
        if all(m["worktree"].get("is_linked_worktree") for m in members):
            continue
        current = [m["workspace_id"] for m in members]
        wanted = sorted_group(members, labels_of, key, order)
        if wanted == current:
            continue
        if dry_run:
            names = {r["workspace_id"]: r.get("label", r["workspace_id"]) for r in members}
            print("%s:" % repo_key)
            for workspace_id in wanted:
                value = labels_of(next(m for m in members if m["workspace_id"] == workspace_id)).get(key, "")
                print("  %-12s %s" % (value or "-", names[workspace_id]))
            continue
        # The sidebar puts a group where its first member is, so put the
        # block back before whatever followed that member.
        ids = [r["workspace_id"] for r in records]
        first = ids.index(current[0])
        before = next((i for i in ids[first + 1:] if i not in current), None)
        params = {"workspace_ids": wanted}
        if before:
            params["before_workspace_id"] = before
        result = socket_request("workspace.move_block", params)
        if result is None:
            ok = False
            continue
        moved = [w for w in result.get("workspaces", []) if isinstance(w, dict)]
        if moved:
            records = moved
    return ok


# --------------------------------------------------------------------------
# Changing labels
# --------------------------------------------------------------------------


def resort_if_sort_key(token):
    """Re-sort after the sort label changed. A failed sort only logs: the
    label itself was saved and published."""
    key, _ = load_sort_config()
    if key and token == key:
        sort_workspaces()


def set_label(workspace_id, token, value):
    checkout = checkout_of(workspace_id)
    key = key_for(workspace_id, checkout)
    state = load_state()
    labels = dict(state.get(key, {}))
    labels[token] = value
    state[key] = labels
    if not save_state(state):
        return False
    ok = publish_token(workspace_id, token, value)
    resort_if_sort_key(token)
    return ok


def clear_label(workspace_id, token):
    checkout = checkout_of(workspace_id)
    key = key_for(workspace_id, checkout)
    state = load_state()
    labels = dict(state.get(key, {}))
    labels.pop(token, None)
    if labels:
        state[key] = labels
    else:
        state.pop(key, None)
    if not save_state(state):
        return False
    ok = publish_token(workspace_id, token, "")
    resort_if_sort_key(token)
    return ok


def clear_all(workspace_id):
    checkout = checkout_of(workspace_id)
    key = key_for(workspace_id, checkout)
    state = load_state()
    labels = state.pop(key, {})
    if not save_state(state):
        return False
    ok = True
    for token in labels:
        if not publish_token(workspace_id, token, ""):
            ok = False
    sort_key, _ = load_sort_config()
    if sort_key in labels:
        resort_if_sort_key(sort_key)
    return ok


def get_label(workspace_id, token):
    checkout = checkout_of(workspace_id)
    labels = load_state().get(key_for(workspace_id, checkout), {})
    if token:
        return labels.get(token, "")
    return labels


# --------------------------------------------------------------------------
# Maintenance
# --------------------------------------------------------------------------


def show_list():
    state = load_state()
    if not state:
        print("no labels (%s)" % state_path())
        return
    for key in sorted(state):
        pairs = ", ".join("%s=%s" % (name, value) for name, value in sorted(state[key].items()))
        print("%s  %s" % (key, pairs))


def prune():
    """Drop entries whose checkout path no longer exists on disk (e.g. the
    worktree was removed). Entries keyed by workspace id (no checkout path
    was ever recorded) are left alone -- they age out on their own once the
    workspace id stops appearing in `workspace list`."""
    state = load_state()
    dropped = []
    kept = {}
    for key, labels in state.items():
        if key.startswith("id:") or os.path.isdir(key):
            kept[key] = labels
        else:
            dropped.append(key)
    if not dropped:
        print("nothing to prune")
        return True
    if not save_state(kept):
        return False
    for key in dropped:
        print("pruned %s" % key)
    return True


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def context_workspace():
    """The workspace an action, event, or direct CLI call applies to.

    herdr's own plugin runner passes its context. A direct CLI call matches the
    current git checkout against each workspace's checkout first, because
    HERDR_WORKSPACE_ID can name the wrong workspace: a shared Codex app-server
    daemon runs every session's commands with the environment of whichever
    pane started it."""
    context = env("HERDR_PLUGIN_CONTEXT_JSON")
    if context:
        return env("HERDR_WORKSPACE_ID") or json_str(context, "workspace_id")
    return checkout_workspace() or env("HERDR_WORKSPACE_ID")


def checkout_workspace():
    """The one workspace whose checkout is the git checkout containing the
    current directory, or "" when there is none or it is ambiguous."""
    import subprocess

    try:
        done = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=HERDR_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    toplevel = done.stdout.decode("utf-8", "replace").strip() if done.returncode == 0 else ""
    if not toplevel:
        return ""
    toplevel = os.path.realpath(toplevel)
    matches = [
        workspace_id
        for workspace_id, checkout in workspaces()
        if checkout and os.path.realpath(checkout) == toplevel
    ]
    return matches[0] if len(matches) == 1 else ""


def usage():
    print(__doc__.strip("\n").split("\n\n")[0], file=sys.stderr)
    return 2


def main(argv):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    option = argv[0] if argv else ""

    if option == "--publish-all":
        ok = publish_all()
        if "--sort" in argv[1:] and not sort_workspaces():
            ok = False
        return 0 if ok else 1

    if option == "--republish":
        workspace = context_workspace()
        if not workspace:
            log("no workspace in context")
            return 1
        ok = republish_workspace(workspace)
        if "--sort" in argv[1:] and not sort_workspaces():
            ok = False
        return 0 if ok else 1

    if option == "sort":
        return 0 if sort_workspaces(dry_run="--dry-run" in argv[1:]) else 1

    if option == "list":
        show_list()
        return 0

    if option == "prune":
        return 0 if prune() else 1

    if option in ("set", "clear", "get") or option == "clear-all":
        workspace = context_workspace()
        if not workspace:
            log("HERDR_WORKSPACE_ID is not set -- run this inside the target herdr pane")
            return 1

        if option == "set":
            if len(argv) != 3:
                print("usage: label.py set <key> <value>", file=sys.stderr)
                return 2
            return 0 if set_label(workspace, argv[1], argv[2]) else 1

        if option == "clear":
            if len(argv) != 2:
                print("usage: label.py clear <key>", file=sys.stderr)
                return 2
            return 0 if clear_label(workspace, argv[1]) else 1

        if option == "clear-all":
            return 0 if clear_all(workspace) else 1

        if option == "get":
            token = argv[1] if len(argv) > 1 else ""
            result = get_label(workspace, token)
            if token:
                print(result)
            elif result:
                for name, value in sorted(result.items()):
                    print("%s=%s" % (name, value))
            else:
                print("no labels for this workspace")
            return 0

    return usage()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
