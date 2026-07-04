"""TDD for mailbox_supervisor（純函式決策核心）。

supervisor＝launchd 常駐 script，對每個註冊 party 每輪決策：
- inbox 有信 + 有 surface + 喚醒閂鎖過期 → WAKE（cmux send 注入固定協定訊息）
- inbox 有信 + 閂鎖新鮮 → NOOP（session 處理中，不轟炸）
- 無 surface（session 不在 cmux）→ 不注入；poller 死則照樣復活（投遞/心跳）
- inbox 空 + poller 死 → RESTART_POLLER（detached、零 token）

被測純函式：
- decide(has_mail, surface, poller_alive, last_wake, now, wake_retry) → 'wake'|'restart_poller'|'noop'
- parse_surface(identify_stdout) → surface uuid | None
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import mailbox_supervisor as sv


# ---------- decide：每 party 決策 ----------

def test_mail_with_surface_and_expired_latch_wakes():
    assert sv.decide(has_mail=True, surface="ABC-UUID", poller_alive=False,
                     last_wake=None, now=1000) == "wake"

def test_mail_with_fresh_latch_is_noop_no_storm():
    # 剛注入過（100s 前 < 600s 窗口）→ 不重複轟炸
    assert sv.decide(has_mail=True, surface="ABC-UUID", poller_alive=False,
                     last_wake=900, now=1000) == "noop"

def test_mail_latch_expired_resends():
    # 注入後 600s 信還在 → 再送一次
    assert sv.decide(has_mail=True, surface="ABC-UUID", poller_alive=False,
                     last_wake=300, now=1000, wake_retry=600) == "wake"

def test_mail_without_surface_revives_poller_only():
    # session 不在 cmux：不注入；poller 死了照樣復活（投遞/心跳不中斷）
    assert sv.decide(has_mail=True, surface=None, poller_alive=False,
                     last_wake=None, now=1000) == "restart_poller"

def test_mail_without_surface_poller_alive_is_noop():
    assert sv.decide(has_mail=True, surface=None, poller_alive=True,
                     last_wake=None, now=1000) == "noop"

def test_wake_takes_precedence_over_poller_restart():
    # 有信有 surface 且 poller 死：先叫人收信，不搶著重啟 poller
    assert sv.decide(has_mail=True, surface="ABC-UUID", poller_alive=False,
                     last_wake=None, now=1000) == "wake"

def test_no_mail_dead_poller_restarts_silently():
    assert sv.decide(has_mail=False, surface="ABC-UUID", poller_alive=False,
                     last_wake=None, now=1000) == "restart_poller"

def test_no_mail_alive_poller_is_noop():
    assert sv.decide(has_mail=False, surface="ABC-UUID", poller_alive=True,
                     last_wake=None, now=1000) == "noop"


# ---------- parse_surface：cmux identify 輸出 → surface uuid ----------

IDENTIFY_OK = '''{
  "caller": {
    "is_browser_surface": false,
    "pane_id": "53D03F6B-5908-4E9B-93D4-64E19A7CDC50",
    "surface_id": "9C447FDD-ACB4-4794-AACC-9477AA3D8060",
    "surface_type": "terminal",
    "window_id": "CBC70EF4-D055-4154-B983-0EADAFAAF867",
    "workspace_id": "181AA22F-A244-4676-80E3-0D241EBB8575"
  }
}'''

def test_parse_surface_extracts_caller_surface_id():
    assert sv.parse_surface(IDENTIFY_OK) == "9C447FDD-ACB4-4794-AACC-9477AA3D8060"

def test_parse_surface_not_in_cmux_returns_none():
    assert sv.parse_surface("") is None
    assert sv.parse_surface("cmux: connection refused") is None

def test_parse_surface_missing_caller_returns_none():
    assert sv.parse_surface('{"focused": {}}') is None


# ---------- should_revive：復活門檻（防假 online / 防污染關閉中的 session） ----------

def test_revive_when_surface_still_exists_in_cmux():
    # session tab 還開著（surface 驗證存在）→ 復活合法
    assert sv.should_revive(surface="ABC", surface_exists=True,
                            last_seen=0, now=99999) is True

def test_no_revive_when_surface_gone():
    # tab 關了 → 不復活（誠實 offline；復活＝假 online + 錯 cwd 污染）
    assert sv.should_revive(surface="ABC", surface_exists=False,
                            last_seen=99998, now=99999) is False

def test_surfaceless_revives_only_when_recently_alive():
    # 不在 cmux 的 session：last_seen 新鮮（剛被殺）→ 復活
    assert sv.should_revive(surface=None, surface_exists=False,
                            last_seen=9000, now=9600,
                            revive_window=1800) is True

def test_surfaceless_long_dead_stays_down():
    # 死很久（session 早關了）→ 不復活——codex/lmirr 這類長眠 entry 永不亂拉
    assert sv.should_revive(surface=None, surface_exists=False,
                            last_seen=1000, now=99999,
                            revive_window=1800) is False


# ---------- parse_socket_password：從 cmux.json（JSONC）讀 socket 密碼 ----------

JSONC = '''{
  "$schema": "https://raw.githubusercontent.com/x/schema.json",
  // 這是註解行
  "automation" : {
    "socketControlMode" : "password",
    "socketPassword" : "SECRET-123"
  },
}'''

def test_parse_socket_password_from_jsonc():
    assert sv.parse_socket_password(JSONC) == "SECRET-123"

def test_parse_socket_password_absent_returns_none():
    assert sv.parse_socket_password('{"automation": {}}') is None
    assert sv.parse_socket_password("not json at all") is None


# ---------- manages：哪些 party 歸 poller-supervisor 管（codex 走 launchd 自己的 adapter） ----------

def test_manages_normal_poller_party():
    assert sv.manages("service-a") is True
    assert sv.manages("dashboard") is True

def test_does_not_manage_codex():
    # codex 用 launchd codex-wake（codex exec resume），沒有 inbox_poller.sh；
    # supervisor 不得幫它「復活 poller」。
    assert sv.manages("codex") is False


# ---------- host-aware wake：wake_of（解析 entry）+ wake_command（依 backend 分派） ----------

def test_wake_of_new_schema():
    e = {"wake": {"backend": "tmux", "target": "agy-abc"}}
    assert sv.wake_of(e) == ("tmux", "agy-abc")

def test_wake_of_legacy_cmux_surface():
    # 舊 entry 只有 cmux_surface → 視為 cmux backend
    e = {"cmux_surface": "9C447FDD"}
    assert sv.wake_of(e) == ("cmux", "9C447FDD")

def test_wake_of_none_when_absent():
    assert sv.wake_of({}) == (None, None)

def test_wake_of_prefers_new_over_legacy():
    e = {"wake": {"backend": "tmux", "target": "agy-x"}, "cmux_surface": "OLD"}
    assert sv.wake_of(e) == ("tmux", "agy-x")

def test_wake_command_cmux():
    cmds = sv.wake_command("cmux", "SURF", "MSG")
    # 應是兩步：send 訊息 + Enter；且都帶 --surface SURF
    assert any(c[:2] == ["cmux", "send"] and "SURF" in c and "MSG" in c for c in cmds)
    assert any("send-key" in c and "SURF" in c for c in cmds)

def test_wake_command_tmux():
    cmds = sv.wake_command("tmux", "agy-x", "MSG")
    # tmux send-keys -t agy-x -l "MSG" 然後 Enter
    assert any(c[:2] == ["tmux", "send-keys"] and "agy-x" in c and "MSG" in c for c in cmds)
    assert any(c[:2] == ["tmux", "send-keys"] and "agy-x" in c and "Enter" in c for c in cmds)

def test_wake_command_none_backend_empty():
    assert sv.wake_command(None, None, "MSG") == []
    assert sv.wake_command("", "", "MSG") == []

def test_wake_command_never_includes_letter_content():
    # 訊息是固定協定訊息，由呼叫端傳入；wake_command 不碰信件內容（這裡只確認它照傳的 msg 用）
    cmds = sv.wake_command("tmux", "agy-x", "FIXED_PROTOCOL_MSG")
    flat = " ".join(" ".join(c) for c in cmds)
    assert "FIXED_PROTOCOL_MSG" in flat
