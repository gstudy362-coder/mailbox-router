"""TDD for the Stop hook — 治本版（supervisor 死人開關）。

架構轉向（2026-07-03）：喚醒改由 supervisor 對 cmux surface 注入，**不再靠 poller
exit → harness 通知**。所以 hook 不再逼 session 重啟那條「注定被 harness SIGTERM」的
poller（那正是無限逼啟循環＋燒 token 的根）。hook 降級為 supervisor 的死人開關：

- supervisor 活著（心跳新鮮）→ 永不 block（poller 死了無所謂，注入會喚醒）。
- supervisor 死了 → block 一次提醒人工重開 supervisor pane（每窗口最多一次，不循環）。

純函式：
- is_participant(card_exists, cwd, registry_cwds) -> bool
- supervisor_alive(hb_mtime, now, window) -> bool
- decide_block(is_participant, supervisor_alive, recently_nudged) -> reason|None
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
import stop_relaunch_poller as h


# --- decide_block：死人開關 -------------------------------------------------

def test_block_when_participant_and_supervisor_dead():
    reason = h.decide_block(is_participant=True, supervisor_alive=False, recently_nudged=False)
    assert reason
    assert "supervisor" in reason.lower()

def test_allow_when_supervisor_alive():
    # supervisor 活著 → poller 死不死都不 block（不再逼啟、無循環）
    assert h.decide_block(is_participant=True, supervisor_alive=True, recently_nudged=False) is None

def test_allow_when_not_participant():
    assert h.decide_block(is_participant=False, supervisor_alive=False, recently_nudged=False) is None

def test_allow_when_recently_nudged():
    # supervisor 死了但剛提醒過 → 不重複轟炸（每窗口一次）
    assert h.decide_block(is_participant=True, supervisor_alive=False, recently_nudged=True) is None


# --- supervisor_alive：心跳新鮮度 -------------------------------------------

def test_supervisor_alive_fresh_heartbeat():
    assert h.supervisor_alive(hb_mtime=1000, now=1100, window=180) is True   # 100s < 180

def test_supervisor_dead_stale_heartbeat():
    assert h.supervisor_alive(hb_mtime=1000, now=1300, window=180) is False  # 300s > 180

def test_supervisor_dead_no_heartbeat():
    assert h.supervisor_alive(hb_mtime=None, now=1000, window=180) is False


# --- is_participant --------------------------------------------------------

def test_participant_via_mailbox_card():
    assert h.is_participant(card_exists=True, cwd="/repo/x", registry_cwds=[]) is True

def test_participant_via_registry_cwd_match():
    assert h.is_participant(card_exists=False, cwd="/repo/x", registry_cwds=["/repo/x"]) is True

def test_not_participant_when_neither():
    assert h.is_participant(card_exists=False, cwd="/repo/x", registry_cwds=["/other"]) is False
