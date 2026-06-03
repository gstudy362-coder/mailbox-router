"""TDD for mailbox_router core (pure functions, delivery-only).

Router 只負責投遞；處理交給各方 live session（inbox_poller 喚醒）。
- parse_headers: TO / THREAD / STATUS from a letter's front-matter
- delivery dedup: msg id from content hash, is_delivered against state
- circuit breaker: 單 thread 往返上限
- thread convergence: 雙方最後一封都 STATUS=done → 靜默
- single-flight: pid_alive 判活（避免併發投遞互踩 state）
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import mailbox_router as r


# ---------- parse_headers ----------

_LETTER = """# RE: something

TO: dashboard
THREAD: lake-parquet
STATUS: needs-reply

body text here
"""


def test_parse_headers_extracts_routing_fields():
    h = r.parse_headers(_LETTER)
    assert h["to"] == "dashboard"
    assert h["thread"] == "lake-parquet"
    assert h["status"] == "needs-reply"


def test_parse_headers_case_insensitive_and_missing_defaults():
    letter = "no headers here\njust body"
    h = r.parse_headers(letter)
    assert h["to"] is None
    assert h["status"] == "needs-reply"   # default when absent


def test_parse_headers_status_done():
    assert r.parse_headers("TO: x\nSTATUS: done\n")["status"] == "done"


# ---------- delivery dedup ----------

def test_message_id_stable_for_same_content():
    assert r.message_id("hello") == r.message_id("hello")
    assert r.message_id("a") != r.message_id("b")


def test_is_delivered_checks_state():
    state = {"delivered": ["abc123"]}
    assert r.is_delivered("abc123", state) is True
    assert r.is_delivered("zzz999", state) is False


# ---------- circuit breaker（單 thread 往返上限）----------

def test_breaker_trips_on_thread_turn_cap():
    state = {"thread_turns": {"lake-parquet": 12}}
    tripped, reason = r.breaker_check("lake-parquet", state, max_turns=12)
    assert tripped is True and "turn" in reason.lower()


def test_breaker_ok_under_cap():
    state = {"thread_turns": {"t": 3}}
    tripped, _ = r.breaker_check("t", state, max_turns=12)
    assert tripped is False


# ---------- thread convergence ----------

def test_thread_converged_when_both_done():
    # last letter from each side has STATUS=done
    assert r.thread_converged(
        last_status_by_party={"service-a": "done", "dashboard": "done"}) is True


def test_thread_not_converged_when_one_needs_reply():
    assert r.thread_converged(
        last_status_by_party={"service-a": "done",
                              "dashboard": "needs-reply"}) is False


def test_thread_not_converged_when_only_one_party_seen():
    assert r.thread_converged(
        last_status_by_party={"service-a": "done"}) is False


# ---------- single-flight (快速連發 --once 不可併發投遞) ----------

def test_pid_alive_true_for_self_false_for_bogus():
    import os
    assert r.pid_alive(os.getpid()) is True
    assert r.pid_alive(2_000_000_000) is False   # 不存在的 PID


# ---------- STAGE lifecycle: stage_rank ----------

def test_stage_rank_orders_nonterminal_stages():
    assert r.stage_rank("ask") == 1
    assert r.stage_rank("accept") == 2
    assert r.stage_rank("deliver") == 3


def test_stage_rank_terminal_stages_are_highest():
    assert r.stage_rank("done") == 4
    assert r.stage_rank("reject") == 4
    assert r.stage_rank("fyi") == 4


def test_stage_rank_block_does_not_advance():
    # block 是「卡住」訊號，rank 0：永遠不抬高水位、也非終結
    assert r.stage_rank("block") == 0


def test_stage_rank_unknown_is_conservative():
    # 未知值不可被當成終結(否則誤收斂)，也不抬水位
    assert r.stage_rank("wat") == 0


# ---------- STAGE lifecycle: parse_stage ----------

def test_parse_stage_explicit_header_wins():
    assert r.parse_stage("TO: dashboard\nSTAGE: deliver\n\nbody") == "deliver"


def test_parse_stage_falls_back_to_legacy_status():
    assert r.parse_stage("STATUS: done\n") == "done"
    assert r.parse_stage("STATUS: fyi\n") == "fyi"
    assert r.parse_stage("STATUS: needs-reply\n") == "ask"


def test_parse_stage_defaults_to_ask_when_absent():
    assert r.parse_stage("no headers here\njust body") == "ask"


def test_parse_stage_invalid_stage_falls_through():
    # STAGE 值不在 enum → 視同沒寫，落到 STATUS / 預設
    assert r.parse_stage("STAGE: bogus\nSTATUS: done\n") == "done"
    assert r.parse_stage("STAGE: bogus\n") == "ask"


# ---------- STAGE lifecycle: record_stage (progress-aware breaker) ----------

def test_record_stage_tracks_latest_stage_per_party():
    state = {}
    r.record_stage(state, "t", "service-a", "accept")
    r.record_stage(state, "t", "service-a", "deliver")
    assert state["thread_stage"]["t"]["service-a"] == "deliver"


def test_record_stage_forward_progress_resets_turns():
    state = {}
    r.record_stage(state, "t", "service-a", "ask")       # rank1 > hwm0 → reset
    r.record_stage(state, "t", "dashboard", "accept")    # rank2 > 1 → reset
    r.record_stage(state, "t", "service-a", "deliver")   # rank3 > 2 → reset
    assert state["thread_turns"]["t"] == 0
    assert state["thread_hwm"]["t"] == 3


def test_record_stage_stall_without_progress_accumulates():
    state = {}
    r.record_stage(state, "t", "service-a", "ask")   # hwm1, turns0
    r.record_stage(state, "t", "dashboard", "ask")   # 1 not > 1 → turns1
    r.record_stage(state, "t", "service-a", "ask")   # → turns2
    assert state["thread_turns"]["t"] == 2
    assert state["thread_hwm"]["t"] == 1


def test_record_stage_regress_does_not_reset():
    state = {}
    r.record_stage(state, "t", "service-a", "deliver")  # hwm3, turns0
    r.record_stage(state, "t", "dashboard", "ask")      # revise: rank1 < 3 → turns1
    assert state["thread_turns"]["t"] == 1
    assert state["thread_hwm"]["t"] == 3


def test_record_stage_one_sided_done_counts_as_progress():
    state = {}
    r.record_stage(state, "t", "service-a", "deliver")  # hwm3, turns0
    r.record_stage(state, "t", "dashboard", "ask")      # turns1
    r.record_stage(state, "t", "service-a", "done")     # rank4 > 3 → reset
    assert state["thread_turns"]["t"] == 0
    assert state["thread_hwm"]["t"] == 4


# ---------- 斷路器：有推進不誤觸發；停滯到上限才 trip ----------

def test_breaker_never_trips_on_forward_progress():
    state = {}
    for party, stage in [("service-a", "ask"), ("dashboard", "accept"),
                         ("service-a", "deliver"), ("dashboard", "done")]:
        r.record_stage(state, "t", party, stage)
        tripped, _ = r.breaker_check("t", state, max_turns=r.MAX_NOPROGRESS_TURNS)
        assert tripped is False


def test_breaker_trips_after_no_progress_cap():
    state = {}
    r.record_stage(state, "t", "service-a", "ask")          # hwm1, turns0
    for _ in range(r.MAX_NOPROGRESS_TURNS):                  # 持續 ask，無推進
        r.record_stage(state, "t", "dashboard", "ask")
    tripped, reason = r.breaker_check("t", state, max_turns=r.MAX_NOPROGRESS_TURNS)
    assert tripped is True and "turn" in reason.lower()


# ---------- STAGE lifecycle: is_converged (stage-aware 收斂) ----------

def test_is_converged_both_terminal_true():
    state = {}
    r.record_stage(state, "t", "service-a", "done")
    r.record_stage(state, "t", "dashboard", "done")
    assert r.is_converged(state, "t") is True


def test_is_converged_mixed_terminal_true():
    state = {}
    r.record_stage(state, "t", "service-a", "done")
    r.record_stage(state, "t", "dashboard", "reject")
    assert r.is_converged(state, "t") is True


def test_is_converged_one_nonterminal_false():
    state = {}
    r.record_stage(state, "t", "service-a", "done")
    r.record_stage(state, "t", "dashboard", "ask")
    assert r.is_converged(state, "t") is False


def test_is_converged_unseen_thread_false():
    assert r.is_converged({}, "never-seen") is False


# ---------- STAGE lifecycle: is_mutual_block (互等死鎖) ----------

def test_is_mutual_block_both_block_true():
    state = {}
    r.record_stage(state, "t", "service-a", "block")
    r.record_stage(state, "t", "dashboard", "block")
    assert r.is_mutual_block(state, "t") is True


def test_is_mutual_block_one_sided_false():
    state = {}
    r.record_stage(state, "t", "service-a", "block")
    r.record_stage(state, "t", "dashboard", "ask")
    assert r.is_mutual_block(state, "t") is False


def test_is_mutual_block_single_party_false():
    state = {}
    r.record_stage(state, "t", "service-a", "block")
    assert r.is_mutual_block(state, "t") is False


# ---------- 投遞路徑整合：記 stage + 互等死鎖告警 ----------

def _tmp_mailboxes(tmp_path, monkeypatch):
    qd = tmp_path / "service-a" / "mailbox"
    db = tmp_path / "dashboard" / "mailbox"
    (qd / "outbox").mkdir(parents=True)
    (db / "outbox").mkdir(parents=True)
    monkeypatch.setattr(r, "PARTIES", {"service-a": qd, "dashboard": db})
    monkeypatch.setattr(r, "STATE_DIR", tmp_path / ".state")
    monkeypatch.setattr(r, "LOG_FILE", tmp_path / ".state" / "router.log")
    return qd, db


def test_deliver_records_stage_into_state(tmp_path, monkeypatch):
    qd, db = _tmp_mailboxes(tmp_path, monkeypatch)
    (qd / "outbox" / "hello.md").write_text(
        "TO: dashboard\nTHREAD: demo\nSTAGE: deliver\n\nhi")
    state = {"delivered": [], "thread_turns": {}}
    r.deliver_new_letters(state, dry_run=False)
    # 投到對方 inbox
    assert (db / "inbox" / "hello.md").exists()
    # stage 記進 state（取代裸 thread_turns += 1）
    assert state["thread_stage"]["demo"]["service-a"] == "deliver"


def test_deliver_alerts_on_mutual_block(tmp_path, monkeypatch):
    qd, db = _tmp_mailboxes(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(r, "_notify", lambda m: calls.append(m))
    # dashboard 已 block，service-a 也送 block → 互等死鎖
    state = {"delivered": [], "thread_turns": {},
             "thread_stage": {"demo": {"dashboard": "block"}}, "thread_hwm": {}}
    (qd / "outbox" / "stuck.md").write_text(
        "TO: dashboard\nTHREAD: demo\nSTAGE: block\n\n卡住了")
    r.deliver_new_letters(state, dry_run=False)
    assert calls, "互等死鎖應觸發告警"
    assert any("block" in c.lower() or "死鎖" in c or "deadlock" in c.lower()
               for c in calls)


# ---------- M2: resolve_parties（seed ⊕ 註冊表，註冊表優先）----------

def test_resolve_parties_no_registry_equals_seed(tmp_path, monkeypatch):
    # 無 registry 目錄 → resolve_parties() 等同 seed PARTIES（向後相容）
    seed = {"service-a": tmp_path / "qd" / "mailbox",
            "dashboard": tmp_path / "db" / "mailbox"}
    monkeypatch.setattr(r, "PARTIES", seed)
    monkeypatch.setattr(r, "STATE_DIR", tmp_path / ".state")  # 尚無 registry/ 子目錄
    assert r.resolve_parties() == seed


def test_resolve_parties_overlays_registry_with_precedence(tmp_path, monkeypatch):
    import registry as reg
    seed = {"service-a": tmp_path / "qd" / "mailbox",
            "dashboard": tmp_path / "db" / "mailbox"}
    state_dir = tmp_path / ".state"
    monkeypatch.setattr(r, "PARTIES", seed)
    monkeypatch.setattr(r, "STATE_DIR", state_dir)
    # 新方 foo（seed 沒有）+ 覆寫 dashboard 的路徑（註冊表優先）
    foo_mbox = tmp_path / "foo" / "mailbox"
    new_db = tmp_path / "db-relocated" / "mailbox"
    reg.write_entry(state_dir, "foo", str(foo_mbox), str(tmp_path), 111, 1000)
    reg.write_entry(state_dir, "dashboard", str(new_db), str(tmp_path), 222, 1000)
    resolved = r.resolve_parties()
    # 新方加入
    assert resolved["foo"] == Path(foo_mbox)
    # 註冊表覆寫 seed（dashboard 指向新路徑，非 seed 的路徑）
    assert resolved["dashboard"] == Path(new_db)
    # seed 未被註冊表覆蓋的方保留
    assert resolved["service-a"] == seed["service-a"]


# ---------- M2: 投遞改吃註冊表（已註冊新方 / 未註冊方）----------

def test_deliver_to_registered_new_party(tmp_path, monkeypatch):
    import registry as reg
    qd, db = _tmp_mailboxes(tmp_path, monkeypatch)
    # 註冊一個 seed 沒有的新方 foo（離線也可投，信等它）
    foo_mbox = tmp_path / "foo" / "mailbox"
    reg.write_entry(tmp_path / ".state", "foo", str(foo_mbox),
                    str(tmp_path), 333, 1000)
    (qd / "outbox" / "to-foo.md").write_text(
        "TO: foo\nTHREAD: greet\nSTAGE: ask\n\nhi foo")
    state = {"delivered": [], "thread_turns": {}}
    r.deliver_new_letters(state, dry_run=False)
    # 投到 foo/inbox
    assert (foo_mbox / "inbox" / "to-foo.md").exists()
    # outbox 已清空（已投遞）
    assert not (qd / "outbox" / "to-foo.md").exists()


def test_deliver_to_unknown_party_holds_outbox_and_alerts(tmp_path, monkeypatch):
    qd, db = _tmp_mailboxes(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(r, "_notify", lambda m: calls.append(m))
    # TO: ghost — 從沒註冊、不在 seed → 不投、留 outbox、告警
    (qd / "outbox" / "to-ghost.md").write_text(
        "TO: ghost\nTHREAD: lost\nSTAGE: ask\n\nanyone there?")
    state = {"delivered": [], "thread_turns": {}}
    r.deliver_new_letters(state, dry_run=False)
    # 信仍躺 sender outbox（未投遞）
    assert (qd / "outbox" / "to-ghost.md").exists()
    # 觸發 user-visible 告警，且訊息點名未知收件方
    assert calls, "未知收件方應觸發告警"
    assert any("ghost" in c.lower() for c in calls)


def test_registered_party_can_send(tmp_path, monkeypatch):
    # N-way 核心：已註冊的新方不只能收，也能寄（掃描迴圈須吃 resolve_parties）
    import registry as reg
    qd, db = _tmp_mailboxes(tmp_path, monkeypatch)
    foo_mbox = tmp_path / "foo" / "mailbox"
    (foo_mbox / "outbox").mkdir(parents=True)
    reg.write_entry(tmp_path / ".state", "foo", str(foo_mbox),
                    str(tmp_path), 444, 1000)
    (foo_mbox / "outbox" / "foo-sends.md").write_text(
        "TO: dashboard\nTHREAD: greet\nSTAGE: ask\n\nfoo here")
    state = {"delivered": [], "thread_turns": {}}
    r.deliver_new_letters(state, dry_run=False)
    assert (db / "inbox" / "foo-sends.md").exists()           # 新方寄出 → 投到 dashboard
    assert not (foo_mbox / "outbox" / "foo-sends.md").exists()  # foo outbox 已清
