"""Mailbox 投遞器 — 跨 session 協調的信件路由（delivery-only）。

service-a ↔ service-b 兩個 Claude live session 透過各自的
mailbox/ 往返協調。本程式只負責「投遞」：掃兩端 outbox/ 的新信 → 複製到對方
inbox/ + 自己 send-copy/ → 移出 outbox。真正讀信／處理／回信由各方自己的 live
session 做（由 inbox_poller.sh 偵測 inbox 有新信 → 喚醒該 session 親自處理）。

本程式不懂業務內容，只認 front-matter 的 TO/THREAD/STATUS 路由標頭，用
sha1 去重 + circuit breaker（單 thread 往返上限）+ single-flight 鎖兜底。

用法：
    python3 mailbox_router.py --once            # 投遞一輪（poller 每 ~60s 呼叫）
    python3 mailbox_router.py --daemon          # 常駐輪詢投遞
    python3 mailbox_router.py --dry-run --once  # 只印不投遞
"""
from __future__ import annotations

import argparse
import difflib
import os
import hashlib
import json
import re
import shutil
import time
from datetime import datetime
from pathlib import Path

import registry  # M1：動態 session 註冊表（read_registry / write_entry / classify）

WORKSPACE = Path(os.environ.get("MAILBOX_WORKSPACE", str(Path("~/claudeworkspace").expanduser())))
# party → repo mailbox 根
PARTIES = {
    "service-a": WORKSPACE / "service-a" / "mailbox",
    "dashboard": WORKSPACE / "service-b" / "mailbox",
}
STATE_DIR = WORKSPACE / "mailbox-router" / ".state"
LOG_FILE = STATE_DIR / "router.log"
MAX_TURNS = 12       # legacy：舊斷路器上限（既有測試以顯式參數沿用）
MAX_NOPROGRESS_TURNS = 20   # 進度感知斷路器：距上次階段推進的封數上限（達此才 trip+告警）

_HDR = re.compile(r"^\s*(TO|THREAD|STATUS)\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)


# ───────────────────────── pure functions (unit-tested) ─────────────────────

def parse_headers(text: str) -> dict:
    """從信件 front-matter 抽 TO/THREAD/STATUS（大小寫不敏感）。
    status 缺省為 needs-reply（保守：預設需回）。"""
    out = {"to": None, "thread": None, "status": "needs-reply"}
    for m in _HDR.finditer(text):
        key = m.group(1).lower()
        out[key] = m.group(2).strip()
    return out


# ───────────────────────── STAGE 生命週期 ─────────────────────
# 信件 stage（從 ~80 封真實語料反推）。rank 供斷路器 high-water-mark 用：
# 推進到更高 rank → 計數歸零；終結集 rank=4；block=0（卡住，不抬水位、非終結）。
_STAGE_RANK = {"block": 0, "ask": 1, "accept": 2, "deliver": 3,
               "done": 4, "reject": 4, "fyi": 4}
DELIVER_RANK = _STAGE_RANK["deliver"]   # 出貨/收尾（rank≥此）即推進，不因終結飽和
TERMINAL_STAGES = frozenset({"done", "reject", "fyi"})


def _header_value(text: str, key: str) -> str | None:
    m = re.search(rf"^\s*{key}\s*:\s*(.+?)\s*$", text, re.IGNORECASE | re.MULTILINE)
    return m.group(1).strip() if m else None


def stage_rank(stage: str) -> int:
    """stage → 進度序。未知值保守對應 0（不抬水位、非終結，避免誤收斂）。"""
    return _STAGE_RANK.get(stage, 0)


def parse_stage(text: str) -> str:
    """信件文字 → 生命週期 stage。
    顯式 STAGE 標頭優先；否則由 legacy STATUS 映射（done→done, fyi→fyi, 其餘→ask）；
    皆無則保守預設 ask（視為待處理，不會誤判收斂）。"""
    raw = _header_value(text, "STAGE")
    if raw and raw.lower() in _STAGE_RANK:
        return raw.lower()
    status = _header_value(text, "STATUS")
    if status:
        s = status.lower()
        return s if s in ("done", "fyi") else "ask"
    return "ask"


def record_stage(state: dict, thread: str, party: str, stage: str) -> None:
    """記錄某方在某 thread 的最新 stage，並更新進度感知斷路器計數。
    投完一封呼叫：turns +1；若此 stage 抬高了 thread 的階段高水位（rank 更高）→
    turns 歸零（代表有推進）。停滯/回頭則 turns 持續累加，達上限由 breaker_check 觸發。"""
    state.setdefault("thread_turns", {})
    state.setdefault("thread_stage", {})
    state.setdefault("thread_hwm", {})
    state["thread_turns"][thread] = state["thread_turns"].get(thread, 0) + 1
    state["thread_stage"].setdefault(thread, {})[party] = stage
    rank = stage_rank(stage)
    raised = rank > state["thread_hwm"].get(thread, 0)
    if raised:
        state["thread_hwm"][thread] = rank
    # 推進 = 抬高水位（raised）或 出貨/收尾信（rank≥deliver）。後者讓「終結後仍活躍
    # 的串流」(hwm 已飽和) 不再誤觸——出一張 widget 就算推進、turns 歸零。只有非出貨
    # 非推進的 ask/accept/block 才累加。推進同時清告警閂鎖（停滯解除 → 可再次告警）。
    if raised or rank >= DELIVER_RANK:
        state["thread_turns"][thread] = 0
        state.get("thread_alerted", {}).pop(thread, None)


def note_breaker_alert(thread: str, state: dict) -> bool:
    """斷路器告警閂鎖：同一條 thread 每個停滯週期只回 True 一次（首次告警），
    其後回 False（抑制重複轟炸），直到 record_stage 因推進清掉閂鎖。"""
    alerted = state.setdefault("thread_alerted", {})
    if alerted.get(thread):
        return False
    alerted[thread] = True
    return True


def note_stray_alert(mid: str, state: dict) -> bool:
    """卡住信（未知收件方／缺 TO）告警閂鎖：同一封信只在首次回 True（告警），
    其後回 False（靜音），避免每輪 router 掃到投不出去的信就重複 _notify（Telegram 轟炸）。
    收斂由 deliver_new_letters 末尾按『本輪仍卡住』prune——信被處理掉後 id 移除，
    同信再現可重新告警一次。"""
    alerted = state.setdefault("stray_alerted", [])
    if mid in alerted:
        return False
    alerted.append(mid)
    return True


BOUNCE_MAX_ROUNDS = 3   # 同一封投不出去的信最多退幾輪（達此即止，避免無限退信）


def bounce_round(mid: str, state: dict) -> int:
    """退信輪次計數：回傳這封信「本次該退的輪次」(1..BOUNCE_MAX_ROUNDS)；達上限回 0（不再退）。
    收斂由 deliver_new_letters 末尾按『本輪仍卡住』prune——信離開 outbox 後計數清除、同信再現重數。"""
    counts = state.setdefault("bounce_count", {})
    n = counts.get(mid, 0)
    if n >= BOUNCE_MAX_ROUNDS:
        return 0
    counts[mid] = n + 1
    return n + 1


def _bounce_body(letter_name: str, to, parties) -> str:
    """退信內文：點名錯誤、指向註冊表；未知收件方另附 difflib 近似正規名建議。"""
    if to:
        sugg = difflib.get_close_matches(to, list(parties), n=2, cutoff=0.5)
        hint = (f"（你是不是要寄給 {' 或 '.join('`' + s + '`' for s in sugg)}？）"
                if sugg else "")
        return (f"你寄的信 '{letter_name}' 的收件人 '{to}' 不在註冊表{hint}。\n"
                f"寄錯人了——請先看註冊表確認正規 name 再寄："
                f"cat {STATE_DIR}/registry/*.json")
    return (f"你寄的信 '{letter_name}' 缺 TO 收件人標頭，無法投遞。\n"
            f"請補上 TO:<正規name>（先看註冊表確認）再寄："
            f"cat {STATE_DIR}/registry/*.json")


def write_bounce(sender_mbox, party: str, letter_name: str, to, parties,
                 rnd: int) -> None:
    """把退信投進『寄件方自己的 inbox』（固定檔名，不在對方 inbox 堆多封）。
    退信 STAGE=reject、TO=寄件方，讓寄件方被正常喚醒、第一時間看到寄錯了。"""
    inbox = sender_mbox / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    content = (f"TO: {party}\nTHREAD: bounce-{letter_name}\nSTAGE: reject\n\n"
               f"【退信 mailbox-router · round {rnd}/{BOUNCE_MAX_ROUNDS}】\n"
               f"{_bounce_body(letter_name, to, parties)}\n")
    (inbox / f"bounced-{letter_name}").write_text(content)


def _handle_undeliverable(state, party, mbox, parties, letter_name, to, mid,
                          dry_run) -> None:
    """未知收件方／缺 TO 的共同處置：退信回寄件方 inbox（≤BOUNCE_MAX_ROUNDS 輪）
    ＋人類 Telegram 告警一次（backstop，寄件方離線時仍有人知道）。原信留 outbox（呼叫端不 unlink）。"""
    label = f"未知收件方 '{to}'" if to else "缺 TO 標頭"
    rnd = bounce_round(mid, state) if not dry_run else 0
    if rnd:
        write_bounce(mbox, party, letter_name, to, parties, rnd)
    fresh = note_stray_alert(mid, state)   # 人類告警閂鎖：每封只發一次
    note = (f"退寄件方 {party} round {rnd}" if rnd
            else ("退信達上限，靜音" if not dry_run else "dry"))
    log(f"UNDELIVERABLE {label} → 留 outbox 不投 {letter_name}（{note}）")
    if fresh and not dry_run:
        _notify(f"mailbox-router: {party} 的信投不出去（{label}，已退回寄件方）：{letter_name}")


def is_converged(state: dict, thread: str) -> bool:
    """stage-aware 收斂：thread 已出現的所有參與方，最新 stage 皆為終結
    （done/reject/fyi）→ True。未見過的 thread → False。"""
    stages = state.get("thread_stage", {}).get(thread, {})
    if not stages:
        return False
    return all(s in TERMINAL_STAGES for s in stages.values())


def is_mutual_block(state: dict, thread: str) -> bool:
    """互等死鎖：thread 有 ≥2 個參與方、且全部最新 stage 都是 block → True。"""
    stages = state.get("thread_stage", {}).get(thread, {})
    return len(stages) >= 2 and all(s == "block" for s in stages.values())


def message_id(content: str) -> str:
    """信件內容 → 穩定 id（sha1 前 12）。"""
    return hashlib.sha1(content.encode("utf-8")).hexdigest()[:12]


def is_delivered(msg_id: str, state: dict) -> bool:
    return msg_id in set(state.get("delivered", []))


def breaker_check(thread: str, state: dict, *, max_turns: int = MAX_TURNS):
    """circuit breaker：單 thread 往返超過上限 → 回 (tripped: bool, reason: str)。"""
    turns = state.get("thread_turns", {}).get(thread, 0)
    if turns >= max_turns:
        return True, f"thread '{thread}' turn cap reached ({turns}/{max_turns})"
    return False, ""


def thread_converged(last_status_by_party: dict) -> bool:
    """thread 是否收斂：兩個 party 都見過、且各自最後一封都 done。"""
    if set(last_status_by_party) != set(PARTIES):
        return False
    return all(s == "done" for s in last_status_by_party.values())


# ───────────────────────── state I/O ─────────────────────

def load_state() -> dict:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    f = STATE_DIR / "state.json"
    state = json.loads(f.read_text()) if f.exists() else {}
    state.setdefault("delivered", [])
    state.setdefault("thread_turns", {})
    state.setdefault("thread_stage", {})
    state.setdefault("thread_hwm", {})
    state.setdefault("stray_alerted", [])
    state.setdefault("bounce_count", {})
    return state


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / "state.json").write_text(json.dumps(state, indent=2, ensure_ascii=False))


def log(msg: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    line = f"{datetime.now().isoformat(timespec='seconds')}  {msg}"
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")
    print(line, flush=True)


# ───────────────────────── routing（投遞）─────────────────────

def resolve_parties() -> dict:
    """收件方解析表＝seed 預設（PARTIES，含 service-a/dashboard 已知路徑）
    ⊕ 註冊表（.state/registry/*.json 的 name→mailbox_path），**註冊表優先**。

    回 {name: Path(mailbox_path)}。無 registry 目錄時退化為 seed（==PARTIES）→
    既有測試 monkeypatch r.PARTIES 仍有效、向後相容。
    每次呼叫即時讀 PARTIES/STATE_DIR（容許 monkeypatch）。"""
    parties = dict(PARTIES)
    for name, entry in registry.read_registry(STATE_DIR).items():
        mbox = entry.get("mailbox_path")
        if mbox:
            parties[name] = Path(mbox)
    return parties


def deliver_new_letters(state: dict, *, dry_run: bool) -> list:
    """掃兩端 outbox/，投遞未投遞的信到對方 inbox/ + 自己 send-copy/。
    回傳被投遞到的收件方清單（去重）。"""
    recipients = set()
    seen_stray = set()   # 本輪掃到的卡住信 id（未知收件方／缺 TO）→ 供告警閂鎖收斂
    parties = resolve_parties()   # seed ⊕ 註冊表（註冊表優先）
    # 掃「所有已知方」的 outbox（含動態註冊方）→ 任意在線 session 都能寄，不只 seed 兩方。
    for party, mbox in parties.items():
        outbox = mbox / "outbox"
        if not outbox.is_dir():
            continue
        for letter in sorted(outbox.glob("*.md")):
            content = letter.read_text()
            mid = message_id(content)
            if is_delivered(mid, state):
                continue
            hdr = parse_headers(content)
            stage = parse_stage(content)
            to = hdr["to"]
            # 缺 TO 標頭 → 不猜收件方（不再 fallback 給另一個 seed 方，那會誤投）。
            # 退信回寄件方 inbox（≤3 輪）＋留 outbox＋人類告警一次（backstop）。
            if not to:
                seen_stray.add(mid)
                _handle_undeliverable(
                    state, party, mbox, parties, letter.name, None, mid, dry_run)
                continue
            # 收件方＝TO:<name> 直接查表（不再寫死兩方收斂）。
            # 未註冊／路徑未知 → 退信回寄件方 inbox（≤3 輪，含近似名建議）＋留 outbox＋人類告警一次。
            if to not in parties:
                seen_stray.add(mid)
                _handle_undeliverable(
                    state, party, mbox, parties, letter.name, to, mid, dry_run)
                continue
            thread = hdr["thread"] or letter.stem
            tripped, reason = breaker_check(thread, state, max_turns=MAX_NOPROGRESS_TURNS)
            if tripped:
                fresh = note_breaker_alert(thread, state)   # 閂鎖：每停滯週期只告警一次
                log(f"BREAKER {reason} → 暫停投遞 {letter.name}"
                    + ("（通知使用者）" if fresh else "（已告警，靜音）"))
                if fresh:
                    _notify(f"mailbox-router circuit breaker: {reason}")
                continue
            inbox = parties[to] / "inbox"
            sendcopy = mbox / "send-copy"
            if dry_run:
                log(f"DRY deliver {party}→{to} thread={thread} {letter.name}")
            else:
                inbox.mkdir(parents=True, exist_ok=True)
                sendcopy.mkdir(parents=True, exist_ok=True)
                shutil.copy2(letter, inbox / letter.name)
                shutil.copy2(letter, sendcopy / letter.name)
                letter.unlink()  # 移出 outbox（已投遞）
                state["delivered"].append(mid)
                record_stage(state, thread, party, stage)
                conv = " converged" if is_converged(state, thread) else ""
                log(f"DELIVER {party}→{to} thread={thread} stage={stage} "
                    f"turns={state['thread_turns'][thread]}{conv} {letter.name}")
                if is_mutual_block(state, thread):
                    log(f"DEADLOCK 互等死鎖 thread={thread}（雙方 block）→ 通知使用者")
                    _notify(f"mailbox-router deadlock: thread '{thread}' 雙方互等(block)，需介入")
            recipients.add(to)
    # 卡住信閂鎖／退信計數收斂：只保留本輪仍卡住的 id；已處理掉的移除 → 同信再現可重新告警/退信。
    if "stray_alerted" in state:
        state["stray_alerted"] = [m for m in state["stray_alerted"] if m in seen_stray]
    if "bounce_count" in state:
        state["bounce_count"] = {m: c for m, c in state["bounce_count"].items()
                                 if m in seen_stray}
    return sorted(recipients)


def pid_alive(pid: int) -> bool:
    import os
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True            # 存在但無權送訊號 → 仍視為活著
    except Exception:
        return False
    return True


def _notify(msg: str) -> None:
    """circuit breaker → Telegram（重用 service-a 的通知，best-effort）。"""
    try:
        import urllib.request, urllib.parse, re as _re
        env = Path.home() / ".claude/channels/telegram/.env"
        m = _re.search(r"TELEGRAM_BOT_TOKEN\s*=\s*(\S+)", env.read_text())
        if not m:
            return
        tok = m.group(1).strip().strip('"').strip("'")
        cm = _re.search(r"TELEGRAM_CHAT_ID\s*=\s*(\S+)", env.read_text())
        import os as _os
        chat_id = (cm.group(1).strip().strip('"').strip("'") if cm
                   else _os.environ.get("MAILBOX_ALERT_CHAT_ID", ""))
        if not chat_id:
            return
        data = urllib.parse.urlencode({"chat_id": chat_id,
                                       "text": "🔌 " + msg}).encode()
        urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/sendMessage",
                               data=data, timeout=20)
    except Exception:
        pass


def tick(state: dict, *, dry_run: bool) -> None:
    deliver_new_letters(state, dry_run=dry_run)
    if not dry_run:
        save_state(state)


LOCK_FILE = STATE_DIR / "router.lock"


def acquire_singleflight() -> bool:
    """單飛：避免快速連發 --once 時多個 router 同時投遞、互踩 state。
    回 True=取得鎖；False=已有活著的 router。"""
    import os
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if LOCK_FILE.exists():
        try:
            pid = int(LOCK_FILE.read_text().strip())
        except Exception:
            pid = None
        if pid and pid_alive(pid):
            return False
    LOCK_FILE.write_text(str(os.getpid()))
    return True


def singleflight_holder() -> str:
    """讀鎖檔持有者 pid（診斷用）：正常回 pid 字串，讀不到/非數字回 '?'。"""
    try:
        t = LOCK_FILE.read_text().strip()
        return t if t.isdigit() else "?"
    except Exception:
        return "?"


def release_singleflight() -> None:
    try:
        LOCK_FILE.unlink()
    except FileNotFoundError:
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--daemon", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--interval", type=int, default=300)
    # deprecated no-op：投遞是唯一模式。保留只為讓「本次清理前啟動、仍在跑」的
    # inbox_poller（會帶此旗標）不致因未知參數報錯；兩端 poller 各自重啟後可刪。
    ap.add_argument("--no-headless", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if not args.dry_run and not acquire_singleflight():
        log(f"skip: another router run is active (single-flight, holder pid={singleflight_holder()})")
        return 0
    try:
        if args.daemon:
            log(f"daemon start (interval={args.interval}s, dry_run={args.dry_run})")
            while True:
                tick(load_state(), dry_run=args.dry_run)
                time.sleep(args.interval)
        else:
            tick(load_state(), dry_run=args.dry_run)
    finally:
        if not args.dry_run:
            release_singleflight()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
