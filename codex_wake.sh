#!/bin/bash
# Codex wake adapter — mailbox-router 的第二種 wake 轉接器。
#
# Claude session 靠 inbox_poller.sh 自我喚醒（poller 退出 → harness 拉回 live session）。
# Codex 沒有等價 harness hook，所以由 launchd watcher（WatchPaths=Codex inbox + StartInterval
# 兜底）在來信時跑本腳本。它用「resume 一條固定 mailbox session」喚醒 Codex：
#
#     codex exec resume <session-id> "<依協定處理你的 inbox>"
#
# 於是 Codex 接續同一條 live session（累積脈絡 + 磁碟逐字稿
# ~/.codex/sessions/.../rollout-<id>.jsonl，可 tail 追蹤它做了什麼），而非每次冷啟陌生 session。
# 首次無 session → 冷啟 codex exec、抓回 session id 持久化；resume 失敗自動退回冷啟。
#
# Codex 是可信夥伴：不加權限牢籠，用它自己的 ~/.codex/config.toml 跑。
# 可追溯 = mailbox 來回信（received/）+ 固定 session 的 rollout 逐字稿。
#
# 用法：codex_wake.sh [name] [mailbox_path]
#   env：MAILBOX_CODEX_CWD 覆寫 Codex 的 workspace root（預設＝mailbox 的上層目錄）。
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
ROUTER="$SCRIPT_DIR/mailbox_router.py"
NAME="${1:-codex}"
MBOX="${2:-$PWD/mailbox}"
CODEX_CWD="${MAILBOX_CODEX_CWD:-$(dirname "$MBOX")}"   # Codex 的 workspace root（-C / 冷啟 cwd）

INBOX="$MBOX/inbox"
SESSION_FILE="$MBOX/.codex_session_id"                 # 持久化的固定 mailbox session id
LOG="$MBOX/.codex_wake.log"
LOCKDIR="$MBOX/.codex_wake.lock"

mkdir -p "$INBOX"

log() { echo "[$(date '+%m-%d %H:%M:%S')] $*" >> "$LOG"; }

# ── 單例鎖（atomic mkdir；沿用 inbox_poller.sh）──────────────────────────────
# launchd 可能 WatchPaths 與 StartInterval 併發，Codex 自己移檔也會再觸發 watcher。
# 鎖保證最多一個 wake 在跑；stale 鎖（holder 已死，如 kill -9）下次安全接管。
acquire_lock() {
  if mkdir "$LOCKDIR" 2>/dev/null; then return 0; fi
  local holder; holder="$(cat "$LOCKDIR/pid" 2>/dev/null || true)"
  if [ -n "$holder" ] && kill -0 "$holder" 2>/dev/null; then return 1; fi
  rm -rf "$LOCKDIR" 2>/dev/null
  mkdir "$LOCKDIR" 2>/dev/null || return 1
  return 0
}
if acquire_lock; then
  echo $$ > "$LOCKDIR/pid"
  trap 'rm -rf "$LOCKDIR"' EXIT
else
  exit 0   # 已有 wake 在跑 → 無事可做
fi

# ── 先投遞，無未處理信即快速退出 ──────────────────────────────────────────────
# 不在此 registry write-self：本 wake 程序短命，用它的 pid 蓋 Codex 註冊 entry 會讓 Codex
# 看起來閃斷。Codex 非常駐，其既有 entry 自理，信留 inbox 等 watcher 喚醒。
python3 "$ROUTER" --once >/dev/null 2>&1 || true

shopt -s nullglob
pending=( "$INBOX"/*.md )
shopt -u nullglob
if [ "${#pending[@]}" -eq 0 ]; then
  exit 0   # WatchPaths 因非信件變動觸發（如移信到 received/）
fi

log "▶ wake: ${#pending[@]} letter(s) in inbox — resuming Codex"

# ── 喚醒 prompt：要 Codex 自己跑 mailbox 協定 ────────────────────────────────
PROMPT="你有新信進入 mailbox inbox。請依 mailbox-router 協定，自己完成整輪處理：
1. 逐封讀 $INBOX/ 下未處理的 .md 信（看 front-matter 的 TO / THREAD / STAGE 與內文）。
2. 依內容做事（code review、開發等，用你自己的判斷與權限）。
3. 回信寫到 $MBOX/outbox/，沿用協定 header（TO / THREAD / STAGE），STAGE 依結果（done / reject / block / fyi…）。
4. 處理完把原信從 inbox 移到 $MBOX/received/。
全部處理完就結束本回合。"

run_codex() {   # $1 = resume <id> | cold
  if [ "$1" = "resume" ]; then
    codex exec resume "$2" "$PROMPT" 2>&1
  else
    codex exec -C "$CODEX_CWD" --skip-git-repo-check "$PROMPT" 2>&1
  fi
}

SID="$(cat "$SESSION_FILE" 2>/dev/null || true)"
if [ -n "$SID" ]; then
  log "resume session=$SID"
  out="$(run_codex resume "$SID")"; rc=$?
  if [ $rc -ne 0 ]; then
    log "resume failed (rc=$rc) — cold-starting a fresh mailbox session"
    SID=""
  fi
fi
if [ -z "$SID" ]; then
  log "cold-start (no/!invalid session)"
  out="$(run_codex cold)"; rc=$?
  newid="$(printf '%s' "$out" | PYTHONPATH="$SCRIPT_DIR" python3 -c 'import sys,codex_wake; print(codex_wake.parse_session_id(sys.stdin.read()) or "")' 2>/dev/null)"
  if [ -n "$newid" ]; then echo "$newid" > "$SESSION_FILE"; log "captured session=$newid"; fi
fi

# 把 Codex 的回信從 outbox flush 到收件方 inbox
python3 "$ROUTER" --once >/dev/null 2>&1 || true
log "◀ wake done (rc=${rc:-?})"
