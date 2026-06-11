"""TDD for the Stop hook that guarantees poller relaunch (pure functions).

The hook itself does I/O (read stdin JSON, stat the .mailbox-card / nudge marker,
pgrep the poller, read the registry); the decision logic is pure and lives here:
- decide_block(is_participant, poller_running, recently_nudged) -> reason|None
- is_participant(card_exists, cwd, registry_cwds) -> bool
- resolve_name(cwd, registry_entries) -> str
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
import stop_relaunch_poller as h


# --- decide_block: the core gate -------------------------------------------

def test_block_when_participant_poller_down_and_not_nudged():
    reason = h.decide_block(is_participant=True, poller_running=False, recently_nudged=False)
    assert reason  # non-empty string instructing the model to relaunch
    assert "run_in_background" in reason

def test_allow_when_not_participant():
    assert h.decide_block(is_participant=False, poller_running=False, recently_nudged=False) is None

def test_allow_when_poller_running():
    assert h.decide_block(is_participant=True, poller_running=True, recently_nudged=False) is None

def test_allow_when_recently_nudged_loop_guard():
    assert h.decide_block(is_participant=True, poller_running=False, recently_nudged=True) is None


# --- is_participant --------------------------------------------------------

def test_participant_via_mailbox_card():
    assert h.is_participant(card_exists=True, cwd="/repo/x", registry_cwds=[]) is True

def test_participant_via_registry_cwd_match():
    assert h.is_participant(card_exists=False, cwd="/repo/x", registry_cwds=["/repo/x"]) is True

def test_not_participant_when_neither():
    assert h.is_participant(card_exists=False, cwd="/repo/x", registry_cwds=["/other"]) is False


# --- resolve_name ----------------------------------------------------------

def test_name_from_registry_cwd_match_wins():
    entries = [{"name": "alpha", "cwd": "/ws/alpha-repo"}]
    assert h.resolve_name("/ws/alpha-repo", entries) == "alpha"

def test_name_fallback_basename_normalized():
    # With an empty SEED_NAME_MAP the name falls back to the normalized basename.
    assert h.resolve_name("/ws/my-service", []) == "my-service"
    assert h.resolve_name("/ws/My_Repo", []) == "my_repo"

def test_seed_name_map_is_honored_when_populated():
    h.SEED_NAME_MAP["example-repo"] = "example"
    try:
        assert h.resolve_name("/ws/example-repo", []) == "example"
    finally:
        del h.SEED_NAME_MAP["example-repo"]
