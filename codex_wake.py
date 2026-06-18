#!/usr/bin/env python3
"""Codex reactive wake adapter — pure helper.

mailbox-router has two wake adapters. Claude sessions use `inbox_poller.sh` (the
poller exits → the harness wakes the live session). Codex has no such harness
hook, so its adapter (`codex_wake.sh`, driven by a launchd watcher on the inbox)
wakes Codex by RESUMING one fixed mailbox session:

    codex exec resume <session-id> "<process your inbox per the protocol>"

Codex thus continues the same live session — same accumulating context and the
same on-disk transcript (`~/.codex/sessions/.../rollout-*.jsonl`) you can tail to
see exactly what it did — rather than cold-starting a stranger each wake. Codex
is a trusted peer here, so the adapter does not cage it; it runs under Codex's
own config.

The first wake has no session yet, so the adapter cold-starts `codex exec` and
captures the new session id from its output. That parse is the one fiddly,
breakage-prone bit, so it lives here as a pure function (unit-tested in
tests/test_codex_wake.py).
"""
import re

_SESSION_ID = re.compile(
    r"session id:\s*"
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})",
    re.IGNORECASE,
)


def parse_session_id(codex_stdout):
    """Extract the session UUID from a `codex exec` run's output, or None.

    `codex exec` prints a header line like `session id: <uuid>`; we persist that
    id and `resume` it on every subsequent wake so Codex keeps one mailbox
    session. Returns the first well-formed UUID following the label, else None.
    """
    m = _SESSION_ID.search(codex_stdout)
    return m.group(1) if m else None
