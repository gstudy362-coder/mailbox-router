#!/bin/bash
# Inbox poller — 便宜的「router」：每 ~60s 投遞 outbox→inbox + 偵測待處理信，
# 偵測到就 exit，由 run_in_background 的 task-notification 喚醒 live session 處理。
#
# 生命週期（避免重複喚醒、又不掉信）：
#   • 信第一次出現      → 記 in-flight(喚醒時間戳) → 喚醒 live session（= new/process）
#   • 信還在 inbox 且 in-flight 未逾時 → 跳過（我正在處理，不重複喚醒）
#   • 信移出 inbox(→received) → in-flight 自動清掉（= close/完成）
#   • 信還在 inbox 但 in-flight 逾時(RETRY_SEC) → 重新喚醒（我沒處理完 → 重試，不卡死）
# 我只需做一件事：處理完把信移到 received/。狀態由本 script 用「在不在 inbox + 時間戳」自管。
#
# 註冊＋心跳：每輪迴圈開頭呼叫 registry.py write-self 寫/更新自己的 entry
#   <repo>/.state/registry/<name>.json = {name, mailbox_path, cwd, pid, last_seen,
#                                          roles, description}
#   → entry 內容（含自宣告能力 roles/description，來源 MAILBOX_ROLES/MAILBOX_DESC
#     或 $PWD/.mailbox-card）全權由 CLI 負責；任何方報到後即被其他方/TUI/watcher
#     看見；last_seen 每輪推進＝保活。每方只寫自己的檔 → 無寫入競爭、無需鎖。
#
# 用法：inbox_poller.sh <name> [mailbox_path]
#   • name：任意 session 名（檔名安全字元）。
#   • mailbox_path：可選；預設 $PWD/mailbox（從該 repo 目錄啟動）。
#   由【該 party 的 live session】自己啟動（run_in_background），才能喚醒自己那條 session。
set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
ROUTER="$SCRIPT_DIR/mailbox_router.py"
REGISTRY="$SCRIPT_DIR/registry.py"   # write-self CLI（entry 寫入由它全權負責，含自宣告能力）
NAME="${1:?usage: inbox_poller.sh <name> [mailbox_path]}"
MBOX="${2:-$PWD/mailbox}"                  # 預設 $PWD/mailbox，可選第二參數覆寫
INBOX="$MBOX/inbox"
INFLIGHT="$MBOX/.poller_inflight"          # 行格式： "<epoch> <basename>"
RETRY_SEC="${RETRY_SEC:-900}"              # 信卡在 inbox 超過此秒數 → 視為我沒處理完、重喚醒
MAX_CYCLES="${MAX_CYCLES:-1440}"           # ~24h 安全上限
mkdir -p "$INBOX"                          # 防 cwd 飄移／信箱未建 → 後續操作有依靠

# ── 同名單實例守門（治本，atomic mkdir 鎖）──────────────────────────────
# 為何用鎖而非 pgrep：兩個同名 poller 並存會搶同一個 .poller_inflight、互相干擾；
# 過去全靠各 session 自律 `pkill` 清理，裸 `pkill -f inbox_poller.sh` 還會誤殺別名
# session 的 poller。用 pgrep 掃描判同名「不可靠」：pgrep -f 會抓到自己命令替換 pipeline
# 的暫態 pre-exec fork（cmdline 仍是父程序的 "inbox_poller.sh NAME"）→ 連「孤身啟動」
# 都可能誤判而自我拒絕；且有 TOCTOU 競態。改用 atomic `mkdir` 鎖住 mailbox（= 真正會被
# 雙實例污染的 .poller_inflight 所在）：mkdir 是原子操作，無自我誤判、無競態。鎖內記
# holder pid；holder 已死（被 kill -9 留下的 stale 鎖）→ 安全接管；退出時 trap 自動釋放。
LOCKDIR="$MBOX/.poller.lock"
acquire_lock() {
  if mkdir "$LOCKDIR" 2>/dev/null; then return 0; fi      # 取得鎖
  local holder; holder="$(cat "$LOCKDIR/pid" 2>/dev/null || true)"
  if [ -n "$holder" ] && kill -0 "$holder" 2>/dev/null; then
    return 1                                               # 有活著的同名 poller → 拒絕
  fi
  rm -rf "$LOCKDIR" 2>/dev/null                            # stale 鎖（holder 不存在）→ 接管
  mkdir "$LOCKDIR" 2>/dev/null || return 1                 # 競爭接管失敗（別人先接管）→ 拒絕
  return 0
}
if acquire_lock; then
  echo $$ > "$LOCKDIR/pid"
  trap 'rm -rf "$LOCKDIR"' EXIT
else
  holder="$(cat "$LOCKDIR/pid" 2>/dev/null || true)"
  echo "❌ 已有同名 poller 在跑（name=$NAME, pid=${holder:-?}）— 拒絕重複啟動，避免雙實例搶 .poller_inflight"
  exit 2
fi

touch "$INFLIGHT"
# 身份行（可診斷性）：任何異常退出時，task output 至少留下 name/pid/mbox/起始時間可比對。
echo "▶ poller name=$NAME pid=$$ mbox=$MBOX start=$(date '+%m-%d %H:%M:%S')"

# host-aware 喚醒目標自報：poller 在該 session 的持久宿主內跑，自主判斷宿主 → 報 (backend,
# target) 給 registry，供 supervisor 注入喚醒。$TMUX → tmux session 名；否則 CMUX_SURFACE_ID →
# cmux surface；都沒有 → 空（走通用底層）。每輪重抓＝session 改名/搬 tab 自癒。
# ⚠ tmux-hosted 的請從 tmux session 內啟動，別從外層觀景窗，否則會報成會 stale 的 surface。
detect_wake() {  # echo "<backend> <target>"
  if [ -n "${TMUX:-}" ]; then
    echo "tmux $(tmux display-message -p '#S' 2>/dev/null)"
  elif [ -n "${CMUX_SURFACE_ID:-}" ]; then
    echo "cmux ${CMUX_SURFACE_ID}"
  else
    local s; s="$(cmux identify --id-format uuids 2>/dev/null | python3 -c 'import json,sys
try: print(json.load(sys.stdin)["caller"]["surface_id"])
except Exception: pass' 2>/dev/null)"
    [ -n "$s" ] && echo "cmux $s" || echo " "
  fi
}

for i in $(seq 1 "$MAX_CYCLES"); do
  # 報到＋保活：由 registry CLI 寫/更新自己的 entry（含自宣告能力 roles/description、
  # last_seen 每輪推進＝保活）。entry 內容全權由 CLI 負責，poller 不再自組 JSON。
  read -r WB WT <<< "$(detect_wake)"
  python3 "$REGISTRY" write-self --name "$NAME" --mailbox "$MBOX" \
    --cwd "$PWD" --pid $$ --wake-backend "${WB:-}" --wake-target "${WT:-}" >/dev/null 2>&1 || true

  python3 "$ROUTER" --once >/dev/null 2>&1   # 投遞（雙向 outbox→inbox）
  now="$(date +%s)"
  wake=0
  next=""                                  # 重建 in-flight：只保留「仍在 inbox」的信
  for f in "$INBOX"/*.md; do
    [ -e "$f" ] || continue
    b="$(basename "$f")"
    prev="$(awk -v b="$b" '$2==b {print $1}' "$INFLIGHT" | tail -1)"
    if [ -z "$prev" ]; then
      wake=1; next="$next$now $b"$'\n'                     # 新信 → 喚醒
    elif [ "$((now - prev))" -gt "$RETRY_SEC" ]; then
      wake=1; next="$next$now $b"$'\n'                     # in-flight 逾時 → 重試喚醒
    else
      next="$next$prev $b"$'\n'                            # 處理中 → 保留時間戳、跳過
    fi
  done
  printf '%s' "$next" > "$INFLIGHT"         # 移出 inbox 的信自動從 in-flight 消失（= 完成）

  if [ "$wake" -eq 1 ]; then
    sleep 5                                  # 讓信落定
    echo "📬 待處理信 (cycle $i, $(date '+%H:%M:%S')):"
    ls "$INBOX"/*.md 2>/dev/null | xargs -n1 basename 2>/dev/null
    exit 0                                    # → task-notification 喚醒 live session
  fi
  sleep 55
done
echo "poller 達 $MAX_CYCLES 輪上限"
