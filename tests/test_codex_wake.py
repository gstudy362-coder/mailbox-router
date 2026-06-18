"""TDD for the Codex reactive wake adapter (pure helpers).

Design (resume + fixed session-id): a launchd watcher detects mail in Codex's
inbox and wakes Codex by resuming ONE fixed mailbox session:

    codex exec resume <session-id> "<process your inbox per the protocol>"

so Codex continues the same live session (context + an on-disk transcript you
can tail) instead of cold-starting a stranger each time. The first wake has no
session yet, so it cold-starts `codex exec` and we capture the new session id
from its output. That capture is the one fiddly, breakage-prone bit, so it is a
pure function tested here:

- parse_session_id(codex_stdout) -> uuid | None
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import codex_wake as cw


HEADER = """\
OpenAI Codex v0.139.0
--------
workdir: /work/codex
model: gpt-5.5
provider: openai
approval: never
sandbox: danger-full-access
session id: 019ecaac-d439-7001-a6d8-b321f1834643
--------
user
你有新信
codex
done
"""


def test_parse_session_id_from_header():
    assert cw.parse_session_id(HEADER) == "019ecaac-d439-7001-a6d8-b321f1834643"

def test_parse_session_id_is_case_and_space_tolerant():
    out = "Session ID:   ABCD1234-0000-0000-0000-000000000000  \nmore"
    assert cw.parse_session_id(out) == "ABCD1234-0000-0000-0000-000000000000"

def test_parse_session_id_returns_none_when_absent():
    assert cw.parse_session_id("no id here\njust text") is None

def test_parse_session_id_returns_first_when_multiple():
    out = "session id: 11111111-1111-1111-1111-111111111111\n" \
          "session id: 22222222-2222-2222-2222-222222222222\n"
    assert cw.parse_session_id(out) == "11111111-1111-1111-1111-111111111111"

def test_parse_session_id_ignores_non_uuid_after_label():
    # a malformed/missing id should not be mistaken for a session id
    assert cw.parse_session_id("session id: (none)\n") is None
