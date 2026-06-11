#!/usr/bin/env python3
"""Stop hook — guarantee a participating session relaunches its wake poller.

Background: the wake poller is a `run_in_background` task that EXITS each time it
wakes the session; the session must relaunch it. Relying on the model to remember
is unreliable, so this hook runs at end-of-turn and, for a participating session
whose poller is not running, BLOCKS the stop and tells the model to relaunch it.
It deliberately does NOT launch the poller itself — only a model-created
run_in_background task emits the task-notification that wakes the session; a
hook-launched detached process could heartbeat but never wake.

Safety:
- scoped to participants only (a `.mailbox-card` in cwd, or a registry entry whose
  cwd matches this session);
- loop guard: blocks at most once per NUDGE_WINDOW (a fresh `.poller_relaunch_nudge`
  marker in the mailbox; also honors stdin `stop_hook_active` if present);
- FAIL OPEN: any error / indeterminate state -> exit 0 (allow the stop). Never trap
  a session in an un-endable turn.

Decision logic is pure and unit-tested in tests/test_stop_hook.py.

Wire it into ~/.claude/settings.json with hooks/install_stop_hook.py.
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROUTER_DIR = Path(__file__).resolve().parent.parent
STATE_DIR = ROUTER_DIR / ".state" / "registry"
NUDGE_WINDOW = 90  # seconds — block at most once per window (loop guard)

# Optional: map a repo directory name -> a stable session <name>, for cases where
# the poller's <name> differs from the repo's directory name. Empty by default;
# the fallback is the normalized directory basename. Add your own entries if needed.
SEED_NAME_MAP = {}


def _norm(s):
    return re.sub(r"[^a-z0-9_-]", "-", s.lower()).strip("-")


def resolve_name(cwd, registry_entries):
    """Name this session's poller runs under: registry cwd-match wins, else seed
    map, else normalized basename."""
    cwd = str(cwd).rstrip("/")
    for e in registry_entries:
        if str(e.get("cwd", "")).rstrip("/") == cwd:
            return e["name"]
    base = os.path.basename(cwd)
    if base in SEED_NAME_MAP:
        return SEED_NAME_MAP[base]
    return _norm(base)


def is_participant(card_exists, cwd, registry_cwds):
    """A session participates if it declares a .mailbox-card or already has a
    registry entry for this working directory."""
    if card_exists:
        return True
    cwd = str(cwd).rstrip("/")
    return any(str(c).rstrip("/") == cwd for c in registry_cwds)


def decide_block(is_participant, poller_running, recently_nudged):
    """Return a reason string to block the stop, or None to allow it."""
    if not is_participant:
        return None
    if poller_running:
        return None
    if recently_nudged:
        return None
    return (
        "Your inbox poller is not running — the next letter will not wake you. "
        "Relaunch it now via the Bash tool with run_in_background:true (see /poller "
        "step 5), then finish this turn."
    )


# --- I/O helpers (fail-open) ------------------------------------------------

def _read_registry_entries():
    entries = []
    try:
        for f in STATE_DIR.glob("*.json"):
            try:
                entries.append(json.loads(f.read_text()))
            except Exception:
                continue
    except Exception:
        pass
    return entries


def _poller_running(name):
    try:
        r = subprocess.run(
            ["pgrep", "-f", "inbox_poller.sh %s" % name],
            capture_output=True, text=True, timeout=5,
        )
        return r.returncode == 0 and r.stdout.strip() != ""
    except Exception:
        return True  # can't tell -> assume running, do NOT block


def _mailbox_for(cwd, entries):
    cwd = str(cwd).rstrip("/")
    for e in entries:
        if str(e.get("cwd", "")).rstrip("/") == cwd:
            mb = e.get("mailbox_path")
            if mb:
                return mb
    return str(Path(cwd) / "mailbox")


def _recently_nudged(mbox):
    try:
        marker = Path(mbox) / ".poller_relaunch_nudge"
        if marker.exists():
            return (time.time() - marker.stat().st_mtime) < NUDGE_WINDOW
    except Exception:
        pass
    return False


def _touch_nudge(mbox):
    try:
        Path(mbox).mkdir(parents=True, exist_ok=True)
        (Path(mbox) / ".poller_relaunch_nudge").touch()
    except Exception:
        pass


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0  # unparseable input -> allow
    try:
        cwd = data.get("cwd") or os.getcwd()
        stop_hook_active = bool(data.get("stop_hook_active", False))
        entries = _read_registry_entries()
        registry_cwds = [e.get("cwd", "") for e in entries]
        card_exists = (Path(cwd) / ".mailbox-card").exists()
        if not is_participant(card_exists, cwd, registry_cwds):
            return 0
        name = resolve_name(cwd, entries)
        mbox = _mailbox_for(cwd, entries)
        recently = stop_hook_active or _recently_nudged(mbox)
        reason = decide_block(is_participant(card_exists, cwd, registry_cwds),
                              _poller_running(name), recently)
        if reason:
            cmd = "bash %s/inbox_poller.sh %s %s" % (ROUTER_DIR, name, mbox)
            reason = reason + "\nCommand: " + cmd
            _touch_nudge(mbox)
            print(json.dumps({"decision": "block", "reason": reason}))
        return 0
    except Exception:
        return 0  # fail open


if __name__ == "__main__":
    sys.exit(main())
