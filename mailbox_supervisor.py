#!/usr/bin/env python3
"""mailbox supervisor — script 側喚醒 + 零 token poller 復活（launchd 常駐）。

背景（openspec change supervisor-script-wake）：喚醒鏈原本唯一入口是「model 親開的
run_in_background poller exit → harness task-notification」，但 harness 環境會殺 poller
（cmux Agent Hibernation 對閒置背景 agent 的 process group 發 SIGTERM、同 session 雙
process reconcile 互清），每次被殺都逼 session 花一整回合補啟——純燒 token。

本 supervisor 用 cmux socket CLI 開出第二個喚醒入口，把兩件事都收到 script 側：
  - inbox 有未處理信（協定不變式：處理完移 received/，故「inbox 有 *.md」＝未處理，
    零記帳）→ 對該 party 的 cmux surface 注入固定協定訊息（cmux send + Enter）＝
    「打字喚醒」live session。帶 per-party 閂鎖（WAKE_RETRY）防轟炸。
  - inbox 空且 poller 死 → detached 重啟 poller（零 token；喚醒已不靠 poller exit，
    detached 合法——poller 退役為純投遞節奏+心跳）。

安全：注入內容永遠是固定協定指示，**絕不夾帶信件內容**（信件是外部輸入，不得變成
對 session 的直接指令）。升級鏈：本 supervisor（秒級~10 分）→ stuck-watcher Telegram
（15 分）→ 人工。cmux 不可達/不在 cmux → 自動退化為 watcher 路徑，不會更糟。

決策核心是純函式（tests/test_supervisor.py）；I/O 邊界薄層可 monkeypatch。

用法：
    python3 mailbox_supervisor.py            # 常駐（launchd KeepAlive）
    python3 mailbox_supervisor.py --once     # 跑一輪即退（手動驗證用）
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent
STATE_DIR = REPO / ".state"
WAKES_FILE = STATE_DIR / "supervisor_wakes.json"
POLLER = REPO / "inbox_poller.sh"

CYCLE = 60          # 秒/輪
# 走 launchd 自有 wake adapter、非 inbox_poller.sh 的 party：supervisor 不幫它復活 poller。
NON_POLLER_PARTIES = frozenset({"codex"})   # codex → launchd com.user.codex-wake
WAKE_RETRY = 600    # 喚醒閂鎖窗口：注入後信仍在，最快這麼久後才重送
REVIVE_WINDOW = 1800  # 無 surface 的 party：last_seen 多新鮮才視為「剛被殺」值得復活

WAKE_MESSAGE = ("你有新信進入 mailbox inbox，請依 /poller 協定處理："
                "讀 inbox → 回信 outbox → 原信移 received/。")


# ───────────────────────── pure functions (unit-tested) ─────────────────────

def manages(name: str) -> bool:
    """此 party 是否由 poller-supervisor 管（codex 等 launchd-adapter party 除外）。"""
    return name not in NON_POLLER_PARTIES


def decide(has_mail: bool, surface, poller_alive: bool, last_wake,
           now: float, wake_retry: float = WAKE_RETRY) -> str:
    """每 party 每輪的決策（純函式）。

    - 有信 + 有 surface：閂鎖過期 → 'wake'（注入喚醒）；新鮮 → 'noop'（處理中）。
      有信時不搶著重啟 poller——先叫人收信（poller 由 session 自己或 inbox 清空後補）。
    - 無 surface（session 不在 cmux）：不注入；poller 死 → 'restart_poller'
      （投遞/心跳不中斷；喚醒交 watcher 升級）。
    - 沒信：poller 死 → 'restart_poller'；否則 'noop'。
    """
    if has_mail and surface:
        if last_wake is None or now - last_wake >= wake_retry:
            return "wake"
        return "noop"
    if not poller_alive:
        return "restart_poller"
    return "noop"


def should_revive(surface, surface_exists: bool, last_seen: float,
                  now: float, revive_window: float = REVIVE_WINDOW) -> bool:
    """poller 復活門檻（純函式）——防「假 online」與「污染關閉中 session 的 entry」。

    - 有 surface：以「surface 是否仍存在於 cmux」為準（tab 開著才復活；關了＝
      session 已走，復活只會謊報 online）。
    - 無 surface（session 不在 cmux）：只在 last_seen 新鮮（剛被殺）時復活；
      長眠 entry（codex/已關 session）永不亂拉。
    """
    if surface:
        return bool(surface_exists)
    return (now - last_seen) < revive_window


def parse_socket_password(cmux_jsonc: str):
    """從 ~/.config/cmux/cmux.json（JSONC，含 // 註解行與尾逗號）抽 socketPassword。

    launchd 生的 supervisor 不是 cmux 血統，socketControlMode=cmuxOnly 會被拒
    （Broken pipe，2026-07-02 實錄）；password 模式下本函式取出密碼供 --password。
    寬鬆解析：正則直取欄位，不整檔 json.loads（JSONC 非法 JSON）。"""
    import re
    m = re.search(r'"socketPassword"\s*:\s*"([^"]+)"', cmux_jsonc or "")
    return m.group(1) if m else None


def parse_surface(identify_stdout: str):
    """`cmux identify --id-format uuids` 輸出 → caller 的 surface uuid，失敗 → None。"""
    try:
        return json.loads(identify_stdout)["caller"]["surface_id"] or None
    except Exception:
        return None


def wake_of(entry):
    """從 registry entry 取喚醒目標 (backend, target)；相容舊 `cmux_surface`。"""
    w = entry.get("wake")
    if isinstance(w, dict) and w.get("backend") and w.get("target"):
        return w["backend"], w["target"]
    s = entry.get("cmux_surface")
    if s:
        return "cmux", s
    return None, None


def wake_command(backend, target, message):
    """依 backend 產生注入的指令序列（純函式）。None/空 → 不注入（[]）。

    tmux：`send-keys -t <session> -l <msg>` 再 `send-keys -t <session> Enter`。
    cmux：`send --surface <s> <msg>` 再 `send-key --surface <s> enter`。
    永不夾帶信件內容——message 由呼叫端傳固定協定訊息。
    """
    if not backend or not target:
        return []
    if backend == "tmux":
        return [["tmux", "send-keys", "-t", target, "-l", message],
                ["tmux", "send-keys", "-t", target, "Enter"]]
    if backend == "cmux":
        return [["cmux", "send", "--surface", target, message],
                ["cmux", "send-key", "--surface", target, "enter"]]
    return []


# ───────────────────────── I/O 邊界（薄層、可 monkeypatch）─────────────────

def known_parties() -> dict:
    """{name: entry_dict}＝registry 全體（含 cmux_surface 欄位若有）。"""
    out = {}
    try:
        for f in (STATE_DIR / "registry").glob("*.json"):
            try:
                e = json.loads(f.read_text())
                if e.get("name") and e.get("mailbox_path"):
                    out[e["name"]] = e
            except Exception:
                continue
    except Exception:
        pass
    return out


def has_unprocessed_mail(mailbox_path) -> bool:
    try:
        return any(Path(mailbox_path, "inbox").glob("*.md"))
    except Exception:
        return False


def poller_alive(name: str) -> bool:
    try:
        r = subprocess.run(["pgrep", "-f", f"inbox_poller.sh {name}"],
                           capture_output=True, text=True, timeout=5)
        return r.returncode == 0 and r.stdout.strip() != ""
    except Exception:
        return True   # 判不出來 → 當活著（保守，不亂動）


_SOCKET_PW = None
def _socket_password():
    """惰性讀取＋快取 cmux socket 密碼（無密碼模式 → None，照常裸呼叫）。"""
    global _SOCKET_PW
    if _SOCKET_PW is None:
        try:
            _SOCKET_PW = parse_socket_password(
                (Path.home() / ".config/cmux/cmux.json").read_text()) or ""
        except OSError:
            _SOCKET_PW = ""
    return _SOCKET_PW or None


def cmux(*args, timeout=10):
    """跑 cmux CLI；回 (rc, stdout)。cmux 不在 → (異常視為失敗, "")。
    失敗時把 stderr 摘要進 log（診斷 launchd 環境問題用）。
    --password 是全域選項，須在子命令之前。"""
    pw = _socket_password()
    argv = ["cmux"] + (["--password", pw] if pw else []) + list(args)
    try:
        r = subprocess.run(argv, capture_output=True, text=True,
                           timeout=timeout)
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "").strip().splitlines()
            log(f"cmux {' '.join(args[:2])} rc={r.returncode} err={err[0][:120] if err else '(無輸出)'}")
        return r.returncode, r.stdout
    except Exception as e:
        log(f"cmux {' '.join(args[:2])} exception: {e}")
        return 1, ""


def _run(argv, timeout=10) -> int:
    """跑一條 argv（tmux/cmux 皆可），回 rc；不存在/例外 → 非 0。"""
    try:
        return subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout).returncode
    except Exception:
        return 1


def inject_wake(backend, target) -> bool:
    """依 backend 對 target 注入固定協定訊息 + Enter（永不夾帶信件內容）。"""
    cmds = wake_command(backend, target, WAKE_MESSAGE)
    if not cmds:
        return False
    return all(_run(argv) == 0 for argv in cmds)


def target_exists(backend, target) -> bool:
    """喚醒目標是否還在（復活/注入前的存在性檢查）。
    cmux：surface 還在（read-screen 唯讀）。tmux：session 還在（has-session）。"""
    if backend == "cmux":
        rc, _ = cmux("read-screen", "--surface", target, "--lines", "1")
        return rc == 0
    if backend == "tmux":
        return _run(["tmux", "has-session", "-t", target]) == 0
    return False


def restart_poller(name: str, mailbox_path: str, cwd: str) -> None:
    """detached 重啟 poller（零 token）。單例鎖擋重複；喚醒不靠它的 exit。

    ⚠ 必須以該 party 自己的 cwd 生成——poller 心跳的 write-self 讀 $PWD 的
    .mailbox-card / 寫 $PWD 為 cwd；用錯 cwd 會污染該 party 的 registry entry
    （2026-07-02 首驗實錄：六個 entry 被本 repo 的 card 蓋掉）。"""
    try:
        # 洗掉 CMUX_*：cmux identify 讀 env（CMUX_SURFACE_ID 等），env 會穿透
        # setsid——不洗的話「從某個 session 的 shell 手動跑 supervisor」spawn 的
        # poller 會把那個 session 的 surface 寫進別 party 的 entry（首驗實錄：
        # 我的 surface 被寫進 service-a → 它的喚醒差點注入到我這）。
        # session 自己啟動的 poller不經此路徑，自報機制不受影響。
        env = {k: v for k, v in os.environ.items() if not k.startswith("CMUX_")}
        subprocess.Popen(
            ["bash", str(POLLER), name, str(mailbox_path)],
            cwd=cwd or None, env=env,
            stdout=open(os.devnull, "w"), stderr=subprocess.STDOUT,
            start_new_session=True,   # 脫離本程序組：supervisor 重啟不陪葬
        )
    except Exception:
        pass


def load_wakes() -> dict:
    try:
        return json.loads(WAKES_FILE.read_text())
    except Exception:
        return {}


def save_wakes(d: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    WAKES_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=1))


def log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')}  {msg}", flush=True)


# ───────────────────────── 主迴圈體（可單測 orchestration）─────────────────

HEARTBEAT = STATE_DIR / "supervisor.heartbeat"

def run_cycle(now: float = None) -> None:
    if now is None:
        now = time.time()
    # 心跳：stop hook 靠這個判「supervisor 是否活著」來決定要不要 block（死人開關）。
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        HEARTBEAT.write_text(str(int(now)))
    except OSError:
        pass
    wakes = load_wakes()
    for name, entry in known_parties().items():
        if not manages(name):
            continue
        mbox = entry["mailbox_path"]
        backend, target = wake_of(entry)          # host-aware：cmux surface / tmux session
        action = decide(
            has_mail=has_unprocessed_mail(mbox),
            surface=target,                        # 有 target 即可注入喚醒（任何 backend）
            poller_alive=poller_alive(name),
            last_wake=wakes.get(name),
            now=now,
        )
        if action == "wake":
            ok = inject_wake(backend, target)
            log(f"WAKE {name} via {backend}:{str(target)[:12]}… {'ok' if ok else 'FAILED (watcher will escalate)'}")
            wakes[name] = now   # 成敗都記：失敗案例交 watcher，勿供本層轟炸
        elif action == "restart_poller":
            # 只復活 cmux 宿主（或 legacy）的 poller；tmux-hosted 的 poller 活在它自己的
            # tmux session 裡（detached 復活會誤報宿主），且其 tmux target 持久、不需復活也能注入。
            if backend != "tmux" and should_revive(
                    target, target_exists(backend, target) if target else False,
                    entry.get("last_seen", 0), now):
                restart_poller(name, mbox, entry.get("cwd", ""))
                log(f"REVIVE poller {name} (detached, zero-token)")
    # 閂鎖清理：inbox 已空的 party 移除（自然重置）
    parties = known_parties()
    wakes = {n: t for n, t in wakes.items()
             if n in parties and has_unprocessed_mail(parties[n]["mailbox_path"])}
    save_wakes(wakes)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    if args.once:
        lock = STATE_DIR / "supervisor.lock"
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            lock.mkdir()            # atomic：搶不到＝已有 supervisor 在跑 → 跳過
        except FileExistsError:
            holder = ""
            try: holder = (lock / "pid").read_text()
            except OSError: pass
            # stale 鎖（持有者已死）→ 接管
            import os as _os
            alive = False
            if holder.isdigit():
                try: _os.kill(int(holder), 0); alive = True
                except OSError: alive = False
            if alive:
                return 0
            import shutil as _sh; _sh.rmtree(lock, ignore_errors=True)
            try: lock.mkdir()
            except FileExistsError: return 0
        try:
            (lock / "pid").write_text(str(os.getpid()))
            run_cycle()
        finally:
            import shutil as _sh; _sh.rmtree(lock, ignore_errors=True)
        return 0
    log(f"mailbox-supervisor start (cycle={CYCLE}s, wake_retry={WAKE_RETRY}s)")
    while True:
        try:
            run_cycle()
        except Exception as e:
            log(f"cycle error: {e}")
        time.sleep(CYCLE)


if __name__ == "__main__":
    raise SystemExit(main())
