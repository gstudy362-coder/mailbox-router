#!/bin/bash
# Regression test for inbox_poller.sh same-name single-instance guard.
#
# Asserts:
#   1) A second poller started with the SAME <name> as a live one refuses
#      immediately with exit code 2 (not the wake code 0, not a loop).
#   2) A poller with a DIFFERENT <name> is NOT refused (gets past the guard
#      into its loop) — the guard must be per-name, never a blanket lock.
#   3) A STALE lock (holder dead, e.g. after kill -9) is taken over, so restarts
#      are never blocked.
#
# Uses throwaway names + /tmp mailboxes; cleans up its own processes and the
# registry entries it creates (named pkill — never touches real-session pollers).
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
SCRIPT="$REPO/inbox_poller.sh"
STATE="$REPO/.state/registry"
TNAME="__guardtest_$$"
DNAME="__guardother_$$"
SNAME="__guardstale_$$"
TMBOX="/tmp/${TNAME}_mbox"
DMBOX="/tmp/${DNAME}_mbox"
SMBOX="/tmp/${SNAME}_mbox"

cleanup() {
  pkill -9 -f "inbox_poller.sh $TNAME" 2>/dev/null
  pkill -9 -f "inbox_poller.sh $DNAME" 2>/dev/null
  pkill -9 -f "inbox_poller.sh $SNAME" 2>/dev/null
  rm -rf "$TMBOX" "$DMBOX" "$SMBOX" \
    "$STATE/${TNAME}.json" "$STATE/${DNAME}.json" "$STATE/${SNAME}.json"
}
fail() { echo "FAIL: $1"; cleanup; exit 1; }
trap cleanup EXIT

mkdir -p "$TMBOX/inbox" "$DMBOX/inbox"

# --- 1) start first same-name instance, wait until it has COMMITTED ---
#     (printed its "▶ poller" identity line = lock acquired). Waiting for the
#     commit — not just process existence — removes a race where the duplicate
#     could win the lock first.
bash "$SCRIPT" "$TNAME" "$TMBOX" >/tmp/${TNAME}_a.log 2>&1 &
seen=0
for i in $(seq 1 20); do
  if grep -q "▶ poller" "/tmp/${TNAME}_a.log" 2>/dev/null; then seen=1; break; fi
  sleep 1
done
[ "$seen" -eq 1 ] || fail "first instance never committed (no ▶ identity line)"

# --- 2) second SAME-name instance must refuse with exit 2 (bounded wait) ---
bash "$SCRIPT" "$TNAME" "$TMBOX" >/tmp/${TNAME}_b.log 2>&1 &
dup=$!
rc=""
for i in $(seq 1 25); do
  if ! kill -0 "$dup" 2>/dev/null; then wait "$dup"; rc=$?; break; fi
  sleep 1
done
if [ -z "$rc" ]; then
  kill -9 "$dup" 2>/dev/null
  fail "duplicate same-name instance did NOT refuse — still running (no guard)"
fi
[ "$rc" -eq 2 ] || fail "duplicate refused with wrong exit code: got $rc, expected 2"

# --- 3) a DIFFERENT name must NOT be refused by the guard ---
#     It must COMMIT (print ▶), proving the lock is per-name, never a blanket lock.
bash "$SCRIPT" "$DNAME" "$DMBOX" >/tmp/${DNAME}.log 2>&1 &
alive=0
for i in $(seq 1 12); do
  if grep -q "▶ poller" "/tmp/${DNAME}.log" 2>/dev/null; then alive=1; break; fi
  sleep 1
done
[ "$alive" -eq 1 ] || fail "different-name poller was wrongly refused or died"

# --- 4) a STALE lock (holder dead) must be taken over, not block startup ---
#     This is the kill -9 restart path: SIGKILL skips the release trap, leaving
#     a lock with a dead pid. The next start must take it over, or restarts break.
mkdir -p "$SMBOX/inbox" "$SMBOX/.poller.lock"
echo 999999 > "$SMBOX/.poller.lock/pid"   # pid > typical pid_max → guaranteed dead
bash "$SCRIPT" "$SNAME" "$SMBOX" >/tmp/${SNAME}.log 2>&1 &
took=0
for i in $(seq 1 12); do
  if grep -q "▶ poller" "/tmp/${SNAME}.log" 2>/dev/null; then took=1; break; fi
  sleep 1
done
[ "$took" -eq 1 ] || fail "stale lock (dead holder) was NOT taken over — restart would be broken"

echo "PASS: same-name refused (exit 2); different name allowed; stale lock taken over"
cleanup
trap - EXIT
exit 0
