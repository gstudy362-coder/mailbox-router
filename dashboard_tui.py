"""唯讀狀態 TUI — 跨 session 協調的一眼總覽（純 stdlib、ANSI 自刷）。

把分散在 registry / router state.json / launchd / 各方 inbox 的狀態，畫成一張
會自動刷新的終端面板。**全程唯讀**：不寫 registry、不寫 state、不投遞、不搬信。

四個區塊（design.md D5/D8）：
  1. sessions：讀 registry/*.json → 每方 online ● / processing ◐ / offline ○。
  2. threads：讀 router state.json 的 thread_stage → 每個未收斂 thread 各方現在
     卡哪個 stage（誰等誰）；收斂 thread（所有方皆 done/reject/fyi）折疊。
  3. router：launchd `com.user.mailbox-router` 狀態（launchctl list 唯讀解析）。
  4. stuck：掃各方 inbox → 仍卡著的信 + 停留多久。

資料→字串的組裝抽成純函式（render_sessions / render_threads / render_stuck），
這是單測面（tests/test_dashboard.py）。main() 只做 I/O 蒐集 + 清屏重畫 + sleep。

用法：
    python3 dashboard_tui.py            # 進入自刷面板（Ctrl-C 結束）
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path
import os

import registry as reg

# router state.json 與 registry 的根（與 mailbox_router.STATE_DIR 對齊）。
WORKSPACE = Path(os.environ.get("MAILBOX_WORKSPACE", str(Path("~/claudeworkspace").expanduser())))
STATE_DIR = WORKSPACE / "mailbox-router" / ".state"
ROUTER_LABEL = "com.user.mailbox-router"

REFRESH = 2          # 自刷間隔（秒）。
CLEAR = "\033[2J\033[H"   # ANSI：清螢幕 + 游標回左上。

# router stage → 終結集（與 mailbox_router.TERMINAL_STAGES 概念一致；
# 此處內聯避免 import router 帶進投遞副作用，維持 TUI 唯讀純讀）。
TERMINAL_STAGES = frozenset({"done", "reject", "fyi"})

# liveness 標記字元：online ● / processing ◐ / offline ○。
_MARK = {"online": "●", "processing": "◐", "offline": "○"}


# ───────────────────────── pure render (unit-tested) ─────────────────────────

def _human_age(seconds) -> str:
    """秒 → 人類可讀停留時長（s / m / h），保留整數，無小數。"""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    return f"{s // 3600}h"


def render_sessions(registry_dict: dict, now: int, has_inflight: dict) -> str:
    """registry → 每方一行的 sessions 區塊字串（純函式）。

    每行：<mark> <name>  <cwd>  seen <age> ago，mark 由 registry.classify 決定
    （online ● / processing ◐ / offline ○）。has_inflight: {name: bool}，缺省 False。

    能力目錄（design.md D4）：entry 有非空 `roles` → 行尾接 `[role1,role2]`；
    有非空 `description` → 次行縮排顯示，當作發信方查的能力目錄。缺省者照舊渲染。
    """
    lines = ["SESSIONS"]
    if not registry_dict:
        lines.append("  (none registered)")
        return "\n".join(lines)
    for name in sorted(registry_dict):
        entry = registry_dict[name]
        state = reg.classify(entry, now, bool(has_inflight.get(name, False)))
        mark = _MARK.get(state, "?")
        cwd = entry.get("cwd", entry.get("mailbox_path", "?"))
        age = _human_age(now - int(entry.get("last_seen", 0)))
        line = f"  {mark} {name}  {cwd}  seen {age} ago"
        roles = entry.get("roles") or []
        if roles:
            line += f"  [{','.join(roles)}]"
        lines.append(line)
        desc = (entry.get("description") or "").strip()
        if desc:
            lines.append(f"      {desc}")
    return "\n".join(lines)


def render_threads(thread_stage: dict) -> str:
    """router 的 thread_stage {thread: {party: stage}} → threads 區塊字串。

    每個**未收斂** thread 一行：各方現在 stage（誰等誰）。收斂 thread（所有
    參與方最新 stage 皆在 {done,reject,fyi}）折疊省略，只在尾端計數。純函式。
    """
    lines = ["THREADS"]
    if not thread_stage:
        lines.append("  (no threads tracked)")
        return "\n".join(lines)
    converged = 0
    shown = 0
    for thread in sorted(thread_stage):
        stages = thread_stage[thread]
        if stages and all(s in TERMINAL_STAGES for s in stages.values()):
            converged += 1
            continue
        parties = ", ".join(f"{p}={stages[p]}" for p in sorted(stages))
        lines.append(f"  {thread}: {parties}")
        shown += 1
    if shown == 0:
        lines.append("  (all converged)")
    if converged:
        lines.append(f"  ({converged} converged, folded)")
    return "\n".join(lines)


def render_stuck(inbox_listing: list) -> str:
    """各方 inbox 卡信清單 [{party,name,age}, ...] → stuck 區塊字串（純函式）。

    每封一行：<party>  <name>  (<human-age>)。空清單 → 友善佔位。
    """
    lines = ["STUCK MAIL"]
    if not inbox_listing:
        lines.append("  (none stuck)")
        return "\n".join(lines)
    for item in inbox_listing:
        party = item.get("party", "?")
        name = item.get("name", "?")
        age = _human_age(item.get("age", 0))
        lines.append(f"  {party}  {name}  ({age})")
    return "\n".join(lines)


# ───────────────────────── I/O 蒐集（main 用，唯讀）─────────────────────────

def _read_thread_stage() -> dict:
    """讀 router state.json 的 thread_stage（純讀；缺檔/壞檔 → 空 dict）。"""
    import json
    f = STATE_DIR / "state.json"
    try:
        data = json.loads(f.read_text())
    except (OSError, ValueError):
        return {}
    ts = data.get("thread_stage", {})
    return ts if isinstance(ts, dict) else {}


def _inbox_files(mailbox_path: str):
    """某方 inbox 的 *.md 信檔清單（唯讀 glob；缺目錄 → 空）。"""
    inbox = Path(mailbox_path) / "inbox"
    if not inbox.is_dir():
        return []
    return sorted(inbox.glob("*.md"))


def _collect_inflight(registry_dict: dict) -> dict:
    """{name: bool}：該方 inbox 是否有 in-flight 信（供 classify 推 processing）。"""
    out = {}
    for name, entry in registry_dict.items():
        out[name] = bool(_inbox_files(entry.get("mailbox_path", "")))
    return out


def _collect_stuck(registry_dict: dict, now: int) -> list:
    """掃各方 inbox → [{party,name,age}]，age = now - 檔 mtime（唯讀）。"""
    listing = []
    for name in sorted(registry_dict):
        entry = registry_dict[name]
        for f in _inbox_files(entry.get("mailbox_path", "")):
            try:
                age = now - int(f.stat().st_mtime)
            except OSError:
                age = 0
            listing.append({"party": name, "name": f.name, "age": age})
    return listing


def render_router_status() -> str:
    """launchd `com.user.mailbox-router` 狀態（launchctl list 唯讀解析）。

    解析 `launchctl list | grep <label>`：欄位 PID\tStatus\tLabel。
    PID 非 '-' → 視為已載入/運行。任何錯誤 → unknown，絕不拋出（TUI 不崩）。
    """
    lines = ["ROUTER (launchd)"]
    try:
        proc = subprocess.run(
            ["launchctl", "list"],
            capture_output=True, text=True, timeout=5,
        )
        row = next((ln for ln in proc.stdout.splitlines()
                    if ROUTER_LABEL in ln), None)
        if row is None:
            lines.append(f"  {ROUTER_LABEL}: not loaded")
        else:
            cols = row.split("\t")
            pid = cols[0] if cols else "?"
            running = pid not in ("-", "?", "")
            state = f"running (pid {pid})" if running else "loaded (idle)"
            lines.append(f"  {ROUTER_LABEL}: {state}")
    except Exception:
        lines.append(f"  {ROUTER_LABEL}: status unknown")
    return "\n".join(lines)


def build_screen(now: int) -> str:
    """蒐集當前狀態（唯讀）→ 組裝四區塊成整頁字串。"""
    registry_dict = reg.read_registry(STATE_DIR)
    inflight = _collect_inflight(registry_dict)
    sections = [
        render_sessions(registry_dict, now, inflight),
        render_threads(_read_thread_stage()),
        render_router_status(),
        render_stuck(_collect_stuck(registry_dict, now)),
    ]
    return "\n\n".join(sections)


# ───────────────────────── main 迴圈（唯讀自刷）─────────────────────────

def main() -> int:
    """ANSI 清屏 → 重畫 → sleep REFRESH 的唯讀自刷迴圈；Ctrl-C 乾淨結束。"""
    try:
        while True:
            now = int(time.time())
            print(CLEAR + build_screen(now), flush=True)
            time.sleep(REFRESH)
    except KeyboardInterrupt:
        print()  # Ctrl-C 後換行，讓 prompt 不黏在面板上
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
