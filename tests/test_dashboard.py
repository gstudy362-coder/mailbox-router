"""TDD for dashboard_tui — 唯讀 TUI 的純資料→字串渲染函式（純函式可單測）。

dashboard_tui 主迴圈唯讀（ANSI 自刷、不寫狀態、不投遞），但把「資料→畫面字串」
的組裝抽成三支純函式，這才是可單測面（design.md D5/D8）：

- render_sessions(registry_dict, now, has_inflight)
    每方一行：online ● / processing ◐ / offline ○、name、repo/cwd、seen Ns ago。
- render_threads(thread_stage)
    每個未收斂 thread 一段：各方現在 stage（誰等誰）；收斂 thread（所有方皆
    在 {done,reject,fyi}）折疊/省略。
- render_stuck(inbox_listing)
    各方 inbox 仍卡著的信 + 停留多久。

純函式：吃 data、回字串，不碰檔案、不碰時鐘。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import dashboard_tui as d


# ───────────────────────── render_sessions ─────────────────────────

def test_render_sessions_online_marker_and_name():
    now = 1_000_000
    reg = {"service-a": {"name": "service-a", "mailbox_path": "/repo/service-a/mailbox",
                         "cwd": "/repo/service-a", "pid": 111, "last_seen": now - 30}}
    out = d.render_sessions(reg, now, has_inflight={})
    # 在線 → ● 實心圈、含名字、含 "seen 30s ago"
    assert "●" in out
    assert "service-a" in out
    assert "30s ago" in out


def test_render_sessions_offline_marker():
    now = 1_000_000
    reg = {"ghost": {"name": "ghost", "mailbox_path": "/repo/ghost/mailbox",
                     "cwd": "/repo/ghost", "pid": 0, "last_seen": now - 10_000}}
    out = d.render_sessions(reg, now, has_inflight={})
    # 過 window + 無 inflight → offline ○ 空心圈
    assert "○" in out
    assert "●" not in out
    assert "◐" not in out


def test_render_sessions_processing_marker_when_inflight():
    now = 1_000_000
    reg = {"busy": {"name": "busy", "mailbox_path": "/repo/busy/mailbox",
                    "cwd": "/repo/busy", "pid": 222, "last_seen": now - 10_000}}
    # 過 window 但 inbox 有 inflight → processing ◐
    out = d.render_sessions(reg, now, has_inflight={"busy": True})
    assert "◐" in out
    assert "busy" in out


def test_render_sessions_shows_repo_cwd():
    now = 1_000_000
    reg = {"a": {"name": "a", "mailbox_path": "/x/a/mailbox",
                 "cwd": "/x/a", "pid": 1, "last_seen": now}}
    out = d.render_sessions(reg, now, has_inflight={})
    # repo/cwd 要可見
    assert "/x/a" in out


def test_render_sessions_one_line_per_session():
    now = 1_000_000
    reg = {
        "a": {"name": "a", "mailbox_path": "/x/a/mailbox", "cwd": "/x/a",
              "pid": 1, "last_seen": now},
        "b": {"name": "b", "mailbox_path": "/x/b/mailbox", "cwd": "/x/b",
              "pid": 2, "last_seen": now},
    }
    out = d.render_sessions(reg, now, has_inflight={})
    body = [ln for ln in out.splitlines() if "a" == _name_of(ln) or "b" == _name_of(ln)]
    # 兩方各佔一行（不嚴格比對標題列，只比對含名字的列）
    assert sum(1 for ln in out.splitlines() if "/x/a" in ln) == 1
    assert sum(1 for ln in out.splitlines() if "/x/b" in ln) == 1


def _name_of(line: str) -> str:
    parts = line.split()
    return parts[1] if len(parts) > 1 else ""


def test_render_sessions_empty_registry_does_not_crash():
    out = d.render_sessions({}, 1_000_000, has_inflight={})
    assert isinstance(out, str)


def test_render_sessions_shows_roles_and_description():
    # 有能力宣告 → 行尾 [role1,role2]、次行縮排 description（design.md D4）。
    now = 1_000_000
    reg = {"service-a": {"name": "service-a", "mailbox_path": "/repo/service-a/mailbox",
                         "cwd": "/repo/service-a", "pid": 111, "last_seen": now - 30,
                         "roles": ["data-ingest", "backfill"],
                         "description": "負責 FinMind 採集與 ingest"}}
    out = d.render_sessions(reg, now, has_inflight={})
    lines = out.splitlines()
    # roles 以 [role1,role2] 形式接在 session 那一行尾
    session_line = next(ln for ln in lines if "service-a" in ln and "/repo/service-a" in ln)
    assert "[data-ingest,backfill]" in session_line
    # description 為次行、縮排、非空
    desc_line = next(ln for ln in lines if "負責 FinMind 採集與 ingest" in ln)
    assert desc_line != desc_line.lstrip(), "description 應縮排"
    # session 行本身不含 description（description 在它自己的次行）
    assert "負責 FinMind" not in session_line


def test_render_sessions_without_roles_or_description_unchanged():
    # 無 roles / description → 與舊輸出完全一致（向後相容）。
    now = 1_000_000
    reg = {"a": {"name": "a", "mailbox_path": "/x/a/mailbox", "cwd": "/x/a",
                 "pid": 1, "last_seen": now - 5}}
    out = d.render_sessions(reg, now, has_inflight={})
    session_line = next(ln for ln in out.splitlines() if "/x/a" in ln)
    # 無方括號能力標記、無額外縮排次行
    assert "[" not in session_line and "]" not in session_line
    # 只有標題列 + 一行 session（無 description 次行）
    body = [ln for ln in out.splitlines() if ln.strip() and ln != "SESSIONS"]
    assert len(body) == 1


def test_render_sessions_empty_roles_list_treated_as_none():
    # roles 為空 list、description 空字串 → 視同未宣告，不顯示。
    now = 1_000_000
    reg = {"a": {"name": "a", "mailbox_path": "/x/a/mailbox", "cwd": "/x/a",
                 "pid": 1, "last_seen": now, "roles": [], "description": ""}}
    out = d.render_sessions(reg, now, has_inflight={})
    session_line = next(ln for ln in out.splitlines() if "/x/a" in ln)
    assert "[" not in session_line
    body = [ln for ln in out.splitlines() if ln.strip() and ln != "SESSIONS"]
    assert len(body) == 1


def test_render_sessions_roles_only_no_description():
    # 只有 roles、無 description → 顯示 [roles]，但無 description 次行。
    now = 1_000_000
    reg = {"a": {"name": "a", "mailbox_path": "/x/a/mailbox", "cwd": "/x/a",
                 "pid": 1, "last_seen": now, "roles": ["frontend"], "description": ""}}
    out = d.render_sessions(reg, now, has_inflight={})
    session_line = next(ln for ln in out.splitlines() if "/x/a" in ln)
    assert "[frontend]" in session_line
    body = [ln for ln in out.splitlines() if ln.strip() and ln != "SESSIONS"]
    assert len(body) == 1, "無 description → 無次行"


# ───────────────────────── render_threads ─────────────────────────

def test_render_threads_shows_nonconverged_thread_stages():
    thread_stage = {
        "lake-parquet": {"service-a": "deliver", "dashboard": "ask"},
    }
    out = d.render_threads(thread_stage)
    assert "lake-parquet" in out
    # 各方現在 stage 可見（誰等誰）
    assert "service-a" in out and "deliver" in out
    assert "dashboard" in out and "ask" in out


def test_render_threads_folds_converged_thread():
    # 所有方皆終結（done/reject/fyi）→ 該 thread 折疊/省略，不出現在主體
    thread_stage = {
        "finished": {"service-a": "done", "dashboard": "reject"},
        "active": {"service-a": "ask", "dashboard": "block"},
    }
    out = d.render_threads(thread_stage)
    assert "active" in out
    # converged 的 thread 名稱不應出現在逐 thread 明細裡
    detail_lines = [ln for ln in out.splitlines() if "finished" in ln and "fold" not in ln.lower()
                    and "converged" not in ln.lower()]
    assert detail_lines == [], "收斂 thread 不該逐項列出"


def test_render_threads_mixed_terminal_is_converged():
    thread_stage = {"t": {"a": "done", "b": "fyi"}}
    out = d.render_threads(thread_stage)
    # t 全終結 → 折疊：不出現在明細
    assert not any("a" in ln and "done" in ln for ln in out.splitlines())


def test_render_threads_one_party_nonterminal_keeps_thread():
    thread_stage = {"t": {"a": "done", "b": "ask"}}
    out = d.render_threads(thread_stage)
    assert "t" in out
    assert "ask" in out


def test_render_threads_empty_does_not_crash():
    out = d.render_threads({})
    assert isinstance(out, str)


# ───────────────────────── render_stuck ─────────────────────────

def test_render_stuck_lists_letter_party_and_age():
    inbox_listing = [
        {"party": "dashboard", "name": "ask-001.md", "age": 1200},
    ]
    out = d.render_stuck(inbox_listing)
    assert "dashboard" in out
    assert "ask-001.md" in out
    # 停留時長可見（人類可讀，例如 20m / 1200s）
    assert "20m" in out or "1200" in out


def test_render_stuck_empty_listing_does_not_crash():
    out = d.render_stuck([])
    assert isinstance(out, str)


def test_render_stuck_multiple_letters_each_line():
    inbox_listing = [
        {"party": "a", "name": "one.md", "age": 60},
        {"party": "b", "name": "two.md", "age": 120},
    ]
    out = d.render_stuck(inbox_listing)
    assert sum(1 for ln in out.splitlines() if "one.md" in ln) == 1
    assert sum(1 for ln in out.splitlines() if "two.md" in ln) == 1


# ───────────────────────── module surface (main is read-only) ──────

def test_main_is_callable_and_module_has_refresh_default():
    # main() 存在且 REFRESH 預設值存在（主迴圈 sleep 用）
    assert callable(d.main)
    assert isinstance(d.REFRESH, (int, float))
