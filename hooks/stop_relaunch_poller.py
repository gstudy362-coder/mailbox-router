#!/usr/bin/env python3
"""Stop hook — supervisor 死人開關（治本版，2026-07-03）。

背景：喚醒原本靠「session 自己的 run_in_background poller exit → harness 通知」。但
**harness 在回合邊界就會 SIGTERM 背景 task**，poller 注定被殺；舊版 hook 逼 session
重啟 poller → 又被殺 → 逼啟 → …… 無限循環燒 token（2026-07-02 事故根因）。

治本：喚醒改由 supervisor（跑在專屬 cmux pane，harness/hibernation 都殺不到）對 session
的 cmux surface 注入。poller 不再是喚醒命脈，愛死就死。因此本 hook **不再逼啟 poller**，
改為只確保「喚醒機制本身（supervisor）活著」：

- 參與 mailbox 的 session 回合結束時，若 supervisor 心跳新鮮 → 放行（poller 死不死無所謂）。
- 若 supervisor 心跳斷了 → block 一次，提醒人工重開 supervisor pane（每 NUDGE_WINDOW 一次，
  不循環——supervisor 在 pane 裡很穩，這個 block 幾乎不會發生）。
- FAIL OPEN：任何錯誤/判不出 → 放行。

決策邏輯純函式、單元測試在 tests/test_stop_hook.py。
"""
import json
import os
import re
import sys
import time
from pathlib import Path

ROUTER_DIR = Path(__file__).resolve().parent.parent
STATE_DIR = ROUTER_DIR / ".state"
REGISTRY_DIR = STATE_DIR / "registry"
HEARTBEAT = STATE_DIR / "supervisor.heartbeat"
NUDGE = STATE_DIR / ".supervisor_down_nudge"
HEARTBEAT_WINDOW = 180   # 心跳超過此秒數視為 supervisor 死
NUDGE_WINDOW = 600       # supervisor 死時，最多每 10 分鐘提醒一次


def is_participant(card_exists, cwd, registry_cwds):
    """A session participates if it declares a .mailbox-card or already has a
    registry entry for this working directory."""
    if card_exists:
        return True
    cwd = str(cwd).rstrip("/")
    return any(str(c).rstrip("/") == cwd for c in registry_cwds)


def supervisor_alive(hb_mtime, now, window=HEARTBEAT_WINDOW):
    """supervisor 心跳是否新鮮（None＝從未心跳＝死）。"""
    if hb_mtime is None:
        return False
    return (now - hb_mtime) < window


def decide_block(is_participant, supervisor_alive, recently_nudged):
    """回 block reason 或 None。死人開關：只有『參與方 且 supervisor 死 且 未剛提醒』才 block。"""
    if not is_participant:
        return None
    if supervisor_alive:
        return None
    if recently_nudged:
        return None
    return (
        "mailbox supervisor 沒在跑——你（及其他 session）不會被信件注入喚醒。"
        "請在一個專屬 cmux 終端 pane 重開它：\n"
        "指令：python3 %s/mailbox_supervisor.py" % ROUTER_DIR
    )


# --- I/O helpers (fail-open) ------------------------------------------------

def _registry_cwds():
    out = []
    try:
        for f in REGISTRY_DIR.glob("*.json"):
            try:
                out.append(json.loads(f.read_text()).get("cwd", ""))
            except Exception:
                continue
    except Exception:
        pass
    return out


def _heartbeat_mtime():
    try:
        return HEARTBEAT.stat().st_mtime
    except OSError:
        return None


def _recently_nudged():
    try:
        if NUDGE.exists():
            return (time.time() - NUDGE.stat().st_mtime) < NUDGE_WINDOW
    except Exception:
        pass
    return False


def _touch_nudge():
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        NUDGE.touch()
    except Exception:
        pass


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    try:
        cwd = data.get("cwd") or os.getcwd()
        card_exists = (Path(cwd) / ".mailbox-card").exists()
        participant = is_participant(card_exists, cwd, _registry_cwds())
        if not participant:
            return 0
        alive = supervisor_alive(_heartbeat_mtime(), time.time())
        reason = decide_block(participant, alive, _recently_nudged())
        if reason:
            _touch_nudge()
            print(json.dumps({"decision": "block", "reason": reason}))
        return 0
    except Exception:
        return 0  # fail open


if __name__ == "__main__":
    sys.exit(main())
