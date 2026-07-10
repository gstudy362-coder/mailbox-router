"""TDD for mailbox_stuck_watcher（純函式 + 告警狀態，alert-only）。

watcher 常駐：每輪先投遞（mailbox_router --once）→ 掃每個已註冊方 inbox →
信卡 > STUCK_THRESHOLD（預設 15 分）→ Telegram 告警「請開 session <name>」；
同信每 REALERT（預設 6h）再提醒；信離開 inbox 自動清告警記錄。只告警不代處理。

被測純函式：
- stuck_letters(inbox_files_with_mtime, now, threshold) → 卡信清單（mtime 老於 threshold）
- letters_to_alert(stuck_keys, alert_state, now, realert) → 本輪該發告警的 key（新卡 / 超過 realert）
- prune_alerts(alert_state, present_keys) → 清掉已離開 inbox 的告警記錄
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import mailbox_stuck_watcher as w


# ---------- stuck_letters（卡信判定，純函式）----------

def test_stuck_letters_nothing_under_threshold():
    # 全部都在 threshold 內 → 無卡信
    now = 1000
    files = [("a.md", 1000), ("b.md", 990), ("c.md", 200)]
    # threshold=900：c 的年齡 800 < 900 仍未卡
    assert w.stuck_letters(files, now=now, threshold=900) == []


def test_stuck_letters_returns_those_over_threshold():
    now = 10_000
    files = [
        ("fresh.md", 9_500),   # age 500  → 未卡
        ("stuck.md", 8_000),   # age 2000 → 卡（>900）
        ("old.md", 100),       # age 9900 → 卡
    ]
    out = w.stuck_letters(files, now=now, threshold=900)
    names = [p for p, _ in out]
    assert "stuck.md" in names
    assert "old.md" in names
    assert "fresh.md" not in names


def test_stuck_letters_boundary_exactly_threshold_not_stuck():
    # 年齡剛好等於 threshold → 還不算卡（嚴格大於才卡，避免邊界抖動誤報）
    now = 2000
    files = [("edge.md", 1100)]   # age = 900 == threshold
    assert w.stuck_letters(files, now=now, threshold=900) == []
    # 再老 1 秒 → 卡
    files2 = [("edge.md", 1099)]  # age = 901 > 900
    assert [p for p, _ in w.stuck_letters(files2, now=now, threshold=900)] == ["edge.md"]


def test_stuck_letters_empty_input():
    assert w.stuck_letters([], now=123, threshold=900) == []


# ---------- letters_to_alert（哪些 key 本輪該告警：新卡 / 超過 realert）----------

def test_letters_to_alert_new_stuck_letter_alerts():
    # 從沒告警過的卡信 → 本輪該告警
    assert w.letters_to_alert(["k1"], alert_state={}, now=1000, realert=21600) == ["k1"]


def test_letters_to_alert_recent_alert_suppressed():
    # 剛在 now-100 告警過、realert=6h → 本輪不重發
    state = {"k1": 900}
    assert w.letters_to_alert(["k1"], alert_state=state, now=1000, realert=21600) == []


def test_letters_to_alert_realert_after_window():
    # 上次告警在 realert 之前（超過 realert）→ 再次告警
    state = {"k1": 1000}
    now = 1000 + 21600 + 1
    assert w.letters_to_alert(["k1"], alert_state=state, now=now, realert=21600) == ["k1"]


def test_letters_to_alert_boundary_exactly_realert_realerts():
    # 距上次告警剛好 == realert → 視為到期，重發（>= 判定）
    state = {"k1": 1000}
    now = 1000 + 21600
    assert w.letters_to_alert(["k1"], alert_state=state, now=now, realert=21600) == ["k1"]


# ---------- prune_alerts（信離開 inbox → 清告警記錄）----------

def test_prune_alerts_removes_absent_keys():
    state = {"gone.md": 100, "still.md": 200}
    pruned = w.prune_alerts(state, present_keys={"still.md"})
    assert "gone.md" not in pruned
    assert pruned["still.md"] == 200


def test_prune_alerts_keeps_all_when_all_present():
    state = {"a": 1, "b": 2}
    pruned = w.prune_alerts(state, present_keys={"a", "b"})
    assert pruned == {"a": 1, "b": 2}


def test_prune_alerts_empty_present_clears_all():
    assert w.prune_alerts({"a": 1}, present_keys=set()) == {}


# ---------- scan_party_inbox（掃單方 inbox → (path, mtime) 清單，真實檔案）----------

def test_scan_party_inbox_lists_md_files_with_mtime(tmp_path):
    inbox = tmp_path / "mailbox" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "one.md").write_text("hi")
    (inbox / "two.md").write_text("yo")
    (inbox / "notes.txt").write_text("ignore me")   # 非 .md → 不列
    out = w.scan_party_inbox(tmp_path / "mailbox")
    names = sorted(p.name for p, _ in out)
    assert names == ["one.md", "two.md"]
    # mtime 是數值
    for _, mt in out:
        assert isinstance(mt, (int, float))


def test_scan_party_inbox_missing_dir_returns_empty(tmp_path):
    # 沒有 inbox 目錄（離線方從沒收過信）→ 空清單，不爆
    assert w.scan_party_inbox(tmp_path / "nope" / "mailbox") == []


# ---------- run_cycle（整合：投遞 + 掃所有方 + 告警 + 清除，注入 deliver/notify）----------

def _letter_key(party, name):
    return f"{party}/{name}"


def test_run_cycle_alerts_on_stuck_letter_and_persists(tmp_path, monkeypatch):
    # 一個已註冊方 foo，inbox 有一封卡信（mtime 很舊）→ 該告警，且記錄落地。
    foo_mbox = tmp_path / "foo" / "mailbox"
    (foo_mbox / "inbox").mkdir(parents=True)
    letter = foo_mbox / "inbox" / "stuck.md"
    letter.write_text("TO: foo\nbody")
    import os
    old = 1_000_000
    os.utime(letter, (old, old))

    state_dir = tmp_path / ".state"
    monkeypatch.setattr(w, "STATE_DIR", state_dir)
    monkeypatch.setattr(w, "WORKSPACE", tmp_path)  # sandbox stray-outbox 掃描，勿讀真實 workspace
    monkeypatch.setattr(w, "ALERTS_FILE", state_dir / "stuck_alerts.json")
    # 註冊表：foo → foo_mbox
    monkeypatch.setattr(w, "known_parties", lambda: {"foo": str(foo_mbox)})
    delivered = []
    monkeypatch.setattr(w, "run_delivery", lambda: delivered.append(True))
    alerts = []
    monkeypatch.setattr(w, "_notify", lambda m: alerts.append(m))

    w.run_cycle(now=old + 100_000)   # age 巨大 > threshold

    assert delivered, "每輪都應先跑投遞"
    assert alerts, "卡信應觸發告警"
    assert any("foo" in a for a in alerts), "告警應點名該方 foo"
    # 記錄落地
    import json
    saved = json.loads((state_dir / "stuck_alerts.json").read_text())
    assert _letter_key("foo", "stuck.md") in saved


def test_run_cycle_no_alert_when_letter_fresh(tmp_path, monkeypatch):
    foo_mbox = tmp_path / "foo" / "mailbox"
    (foo_mbox / "inbox").mkdir(parents=True)
    letter = foo_mbox / "inbox" / "fresh.md"
    letter.write_text("hi")   # 剛寫入 → mtime≈now → 不卡

    state_dir = tmp_path / ".state"
    monkeypatch.setattr(w, "STATE_DIR", state_dir)
    monkeypatch.setattr(w, "WORKSPACE", tmp_path)  # sandbox stray-outbox 掃描，勿讀真實 workspace
    monkeypatch.setattr(w, "ALERTS_FILE", state_dir / "stuck_alerts.json")
    monkeypatch.setattr(w, "known_parties", lambda: {"foo": str(foo_mbox)})
    monkeypatch.setattr(w, "run_delivery", lambda: None)
    alerts = []
    monkeypatch.setattr(w, "_notify", lambda m: alerts.append(m))

    import time
    w.run_cycle(now=int(time.time()))
    assert alerts == [], "新鮮信不該告警"


def test_run_cycle_clears_alert_when_letter_gone(tmp_path, monkeypatch):
    # 上輪有卡信告警記錄，這輪信已不在 inbox → 記錄被清除。
    foo_mbox = tmp_path / "foo" / "mailbox"
    (foo_mbox / "inbox").mkdir(parents=True)   # inbox 空（信已被處理移走）

    state_dir = tmp_path / ".state"
    state_dir.mkdir(parents=True)
    import json
    (state_dir / "stuck_alerts.json").write_text(
        json.dumps({_letter_key("foo", "gone.md"): 12345}))

    monkeypatch.setattr(w, "STATE_DIR", state_dir)
    monkeypatch.setattr(w, "WORKSPACE", tmp_path)  # sandbox stray-outbox 掃描，勿讀真實 workspace
    monkeypatch.setattr(w, "ALERTS_FILE", state_dir / "stuck_alerts.json")
    monkeypatch.setattr(w, "known_parties", lambda: {"foo": str(foo_mbox)})
    monkeypatch.setattr(w, "run_delivery", lambda: None)
    monkeypatch.setattr(w, "_notify", lambda m: None)

    w.run_cycle(now=99999)
    saved = json.loads((state_dir / "stuck_alerts.json").read_text())
    assert _letter_key("foo", "gone.md") not in saved, "信離開 inbox → 清除告警記錄"


def test_run_cycle_never_moves_or_reads_letters(tmp_path, monkeypatch):
    # alert-only 紀律：偵測卡信後信原封不動（不移、不刪）。
    foo_mbox = tmp_path / "foo" / "mailbox"
    (foo_mbox / "inbox").mkdir(parents=True)
    letter = foo_mbox / "inbox" / "stuck.md"
    letter.write_text("content stays")
    import os
    old = 1_000_000
    os.utime(letter, (old, old))

    state_dir = tmp_path / ".state"
    monkeypatch.setattr(w, "STATE_DIR", state_dir)
    monkeypatch.setattr(w, "WORKSPACE", tmp_path)  # sandbox stray-outbox 掃描，勿讀真實 workspace
    monkeypatch.setattr(w, "ALERTS_FILE", state_dir / "stuck_alerts.json")
    monkeypatch.setattr(w, "known_parties", lambda: {"foo": str(foo_mbox)})
    monkeypatch.setattr(w, "run_delivery", lambda: None)
    monkeypatch.setattr(w, "_notify", lambda m: None)

    w.run_cycle(now=old + 100_000)
    # 信仍在原處、內容不變
    assert letter.exists()
    assert letter.read_text() == "content stays"


# ---------- known_parties：掃描範圍 = seed ⊕ 註冊表（卸舊不留空窗）----------

def test_known_parties_includes_seed_even_with_empty_registry(tmp_path, monkeypatch):
    # 即使註冊表空（沒人用新 poller 報到），watcher 仍須涵蓋 seed（service-a/dashboard），
    # 否則卸掉舊的 service-a 專屬 watcher 會留下告警空窗。
    import mailbox_router as mr
    monkeypatch.setattr(mr, "STATE_DIR", tmp_path / ".state")  # 無 registry/ → resolve==seed
    kp = w.known_parties()
    assert "service-a" in kp and "dashboard" in kp


# ---------- stray outbox（非註冊路徑的 outbox 滯留信告警）----------

REG = ["/ws/service-a/mailbox", "/ws/mailbox-router/mailbox"]

def test_stray_letter_over_threshold_is_flagged():
    now = 10_000
    files = [("/ws/service-a/finmind_replacement/mailbox/outbox/lost.md", 8_000)]
    out = w.stray_outbox_letters(files, REG, now=now, threshold=900)
    assert [p for p, _ in out] == ["/ws/service-a/finmind_replacement/mailbox/outbox/lost.md"]

def test_registered_outbox_is_never_stray():
    # 合法 outbox 裡等投遞的信（就算老）不是 stray
    now = 10_000
    files = [("/ws/service-a/mailbox/outbox/waiting.md", 100)]
    assert w.stray_outbox_letters(files, REG, now=now, threshold=900) == []

def test_stray_letter_under_threshold_not_yet_flagged():
    now = 10_000
    files = [("/ws/foo/sub/mailbox/outbox/fresh.md", 9_500)]   # age 500 < 900
    assert w.stray_outbox_letters(files, REG, now=now, threshold=900) == []

def test_stray_key_prefix():
    assert w.stray_key("/x/mailbox/outbox/a.md").startswith("stray/")
