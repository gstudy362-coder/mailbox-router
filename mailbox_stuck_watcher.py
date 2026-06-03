"""註冊表驅動卡信 watcher — 常駐告警，alert-only（不代處理）。

根治「當機 / session 結束後喚醒層斷、信默默卡在 inbox」的盲點（service-a 端
專屬版只盯一方；本版吃 registry 掃**所有**已註冊方）。

每輪 run_cycle：
  1. run_delivery()：`python3 mailbox_router.py --once`（持續投遞、冪等）。
  2. read_registry()：拿到**每個**已註冊方的 mailbox_path。
  3. 掃每方 inbox → 信卡 > STUCK_THRESHOLD（預設 15 分）→ Telegram 告警
     「請開 session <name> 收信」；同信每 REALERT（預設 6h）再提醒。
  4. 信離開 inbox（被該方 session 親自處理移走）→ 自動清該信告警記錄。

只告警、絕不讀 / 移 / 回信（守 delivery-only 紀律 — 讀信／處理由各方 live
session 親自做）。告警記錄 per-letter 存 `.state/stuck_alerts.json`。

純函式（stuck_letters / letters_to_alert / prune_alerts）抽出可單測；I/O 邊界
（掃 inbox、跑投遞、發 Telegram、讀寫告警記錄）薄薄一層、可 monkeypatch。

用法：
    python3 mailbox_stuck_watcher.py            # 常駐（launchd KeepAlive）
    python3 mailbox_stuck_watcher.py --once     # 跑一輪即退（手動驗證用）
"""
from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path
import os
from typing import List, Tuple

from registry import read_registry  # M1：吃註冊表拿所有方 mailbox_path

WORKSPACE = Path(os.environ.get("MAILBOX_WORKSPACE", str(Path("~/claudeworkspace").expanduser())))
REPO = WORKSPACE / "mailbox-router"
ROUTER = REPO / "mailbox_router.py"
STATE_DIR = REPO / ".state"
ALERTS_FILE = STATE_DIR / "stuck_alerts.json"

# 卡信門檻：信在 inbox 停留超過此秒數 → 視為卡住、告警（預設 15 分）。
STUCK_THRESHOLD = 900
# 重發間隔：同一封仍卡住，距上次告警超過此秒數 → 再提醒一次（預設 6h）。
REALERT = 6 * 60 * 60
# 常駐輪詢間隔（約 2 分鐘掃一次；launchd KeepAlive 兜底跨機重開）。
CYCLE_INTERVAL = 120


# ───────────────────────── pure functions (unit-tested) ─────────────────────

def stuck_letters(inbox_files_with_mtime: List[Tuple[object, float]],
                  now: float, threshold: float) -> list:
    """卡信判定（純函式）。

    inbox_files_with_mtime：`[(path_or_name, mtime), ...]`。
    回傳年齡（now - mtime）**嚴格大於** threshold 的項（原樣保留 (key, mtime)）。
    邊界＝剛好等於 threshold → 不算卡（避免邊界抖動誤報）。
    """
    return [(key, mtime) for key, mtime in inbox_files_with_mtime
            if now - mtime > threshold]


def letters_to_alert(stuck_keys: List[str], alert_state: dict,
                     now: float, realert: float) -> list:
    """本輪該發告警的 key（純函式）。

    - 從沒告警過的卡信（key 不在 alert_state）→ 該告警（新卡）。
    - 距上次告警 >= realert → 再次告警（到期重發；邊界＝剛好 realert 也發）。
    - 否則（剛告警過、還在 realert 內）→ 抑制。
    """
    out = []
    for key in stuck_keys:
        last = alert_state.get(key)
        if last is None or now - last >= realert:
            out.append(key)
    return out


def prune_alerts(alert_state: dict, present_keys) -> dict:
    """信離開 inbox → 清告警記錄（純函式）。

    只保留 key 仍在 present_keys（本輪 inbox 還在的卡信 key）的記錄，回新 dict。
    """
    present = set(present_keys)
    return {k: v for k, v in alert_state.items() if k in present}


def letter_key(party: str, name: str) -> str:
    """告警記錄的 per-letter key：`<party>/<檔名>`（同名信跨方不互撞）。"""
    return f"{party}/{name}"


# ───────────────────────── I/O 邊界（薄層、可 monkeypatch）─────────────────

def scan_party_inbox(mailbox_path) -> list:
    """掃某方 mailbox/inbox → `[(Path, mtime), ...]`（只認 *.md）。

    inbox 目錄不存在（離線方從沒收過信）→ 空清單（不爆）。只讀 stat，不開檔。
    """
    inbox = Path(mailbox_path) / "inbox"
    if not inbox.is_dir():
        return []
    out = []
    for f in sorted(inbox.glob("*.md")):
        try:
            out.append((f, f.stat().st_mtime))
        except OSError:
            continue
    return out


def load_alerts() -> dict:
    """讀 stuck_alerts.json（{letter_key: last_alert_epoch}）；缺/壞 → 空 dict。"""
    try:
        return json.loads(ALERTS_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_alerts(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    ALERTS_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def run_delivery() -> None:
    """跑一輪投遞（`python3 mailbox_router.py --once`），冪等、忽略輸出、best-effort。"""
    try:
        subprocess.run(["python3", str(ROUTER), "--once"],
                       cwd=str(REPO), capture_output=True, timeout=120)
    except Exception:
        pass


def known_parties() -> dict:
    """要掃的所有方 = seed ⊕ 註冊表（與 router 投遞同一視圖 resolve_parties）。
    回 {name: mailbox_path(str)}。確保 service-a/dashboard 等 seed 即使尚未用新
    poller 報到也被涵蓋——卸掉舊的專屬 watcher 不會留下告警空窗。"""
    import mailbox_router
    return {name: str(p) for name, p in mailbox_router.resolve_parties().items()}


def _notify(msg: str) -> None:
    """Telegram 告警（重用 mailbox_router 的通知途徑，best-effort）。"""
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
                                       "text": "📬 " + msg}).encode()
        urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/sendMessage",
                               data=data, timeout=20)
    except Exception:
        pass


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')}  {msg}"
    print(line, flush=True)


# ───────────────────────── 主迴圈體（可單測）─────────────────────

def run_cycle(now: float = None) -> None:
    """一輪：投遞 → 掃所有已註冊方 inbox → 卡信告警 → 清除已離開的記錄。

    為可測性把迴圈體抽成函式；real I/O 點（run_delivery / read_registry /
    scan_party_inbox / _notify / save_alerts）皆可 monkeypatch。
    """
    if now is None:
        now = time.time()

    # 1. 持續投遞（冪等）。
    run_delivery()

    # 2. 取所有方 mailbox_path = seed ⊕ 註冊表（涵蓋未報到的 seed，卸舊不留空窗）。
    parties = known_parties()

    # 3. 掃每方 inbox，收集本輪「仍在 inbox 的卡信 key」。
    alert_state = load_alerts()
    present_stuck = {}          # letter_key → mtime（本輪卡信）
    for name, mbox in parties.items():
        if not mbox:
            continue
        files = scan_party_inbox(mbox)
        for path, mtime in stuck_letters(files, now=now, threshold=STUCK_THRESHOLD):
            key = letter_key(name, Path(path).name)
            present_stuck[key] = (name, mtime)

    # 4. 哪些該本輪告警（新卡 / 超過 REALERT）。
    to_alert = letters_to_alert(list(present_stuck.keys()), alert_state,
                                now=now, realert=REALERT)
    for key in to_alert:
        name, mtime = present_stuck[key]
        age_min = int((now - mtime) // 60)
        _notify(f"信卡在 {name} inbox 約 {age_min} 分鐘未處理 — "
                f"請開 session `{name}` 收信。({key})")
        log(f"ALERT stuck {key} age={age_min}min")
        alert_state[key] = now

    # 5. 信離開 inbox（被處理移走）→ 清該信告警記錄。
    pruned = prune_alerts(alert_state, present_keys=present_stuck.keys())
    save_alerts(pruned)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true",
                    help="跑一輪即退（手動驗證 / 端到端測試用）")
    ap.add_argument("--interval", type=int, default=CYCLE_INTERVAL)
    args = ap.parse_args()

    if args.once:
        run_cycle()
        return 0

    log(f"stuck-watcher start (threshold={STUCK_THRESHOLD}s, "
        f"realert={REALERT}s, interval={args.interval}s)")
    while True:
        try:
            run_cycle()
        except Exception as e:                # 常駐絕不因單輪例外退出
            log(f"cycle error: {e!r}")
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
