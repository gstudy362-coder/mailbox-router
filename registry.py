"""Session 註冊表 — 跨 session 協調的動態註冊核心（純 stdlib）。

每方 live session 在 poller 每輪迴圈寫/更新自己的一檔
`<state_dir>/registry/<name>.json`：

    {"name": str, "mailbox_path": str, "cwd": str,
     "pid": int, "last_seen": int(epoch)}

每方只寫自己的檔 → 無寫入競爭、無需鎖。entry 離線也保留 → mailbox_path
永遠已知 → 對離線方仍可投遞（信躺 inbox 等它）。

本模組全是純函式 + 小量檔案 I/O（讀整個 registry / 寫單檔），不碰投遞、
不碰 state.json，便於單測（tmp_path）。
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import List, Optional

# liveness window：now - last_seen <= 此值 → online（約 3 個 poller 心跳）。
LIVENESS_WINDOW = 180

# 預設 state 目錄：本模組自己的 .state（可由 MAILBOX_STATE_DIR 覆寫，便於測試）。
STATE_DIR = Path(os.environ.get(
    "MAILBOX_STATE_DIR", Path(__file__).resolve().parent / ".state"))

# 檔名安全 charset：保留 [a-z0-9_-]，其餘（大寫先轉小寫後）一律收斂成 '-'。
_UNSAFE = re.compile(r"[^a-z0-9_-]+")


def _normalize_name(name: str) -> str:
    """name → 檔名安全形式 [a-z0-9_-]+：小寫化、非法字元壓成單一 '-'、修邊。"""
    safe = _UNSAFE.sub("-", name.strip().lower()).strip("-")
    return safe


# description 單行上限（截斷）。
_DESC_MAX = 200


def _card_line(card_text: Optional[str], key: str) -> str:
    """從 .mailbox-card 文字取 `<key>:` 行的值（單行）。

    跳過 '#' 開頭的註解行；找不到回 ""。只取第一個命中的 key 行。
    """
    if not card_text:
        return ""
    prefix = key + ":"
    for raw in card_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    return ""


def parse_roles(env_val: Optional[str], card_text: Optional[str]) -> List[str]:
    """解析本方 roles：env 優先，否則 card 的 `roles:` 行；缺省 []。

    來源（逗號分隔字串）→ 逐項修邊、丟空、正規化 [a-z0-9_-]（同 name）、
    去重且保留順序。正規化後變空的項一併丟掉。
    """
    src = env_val.strip() if env_val and env_val.strip() else _card_line(
        card_text, "roles")
    out: List[str] = []
    seen = set()
    for part in src.split(","):
        role = _normalize_name(part)
        if not role or role in seen:
            continue
        seen.add(role)
        out.append(role)
    return out


def parse_description(env_val: Optional[str],
                      card_text: Optional[str]) -> str:
    """解析本方 description：env 優先，否則 card 的 `desc:` 行；缺省 ""。

    單行自由文字，截斷上限 _DESC_MAX 字。
    """
    src = env_val.strip() if env_val and env_val.strip() else _card_line(
        card_text, "desc")
    return src[:_DESC_MAX]


def registry_path(state_dir, name: str) -> Path:
    """某方 registry json 的路徑：<state_dir>/registry/<normalized-name>.json。"""
    return Path(state_dir) / "registry" / f"{_normalize_name(name)}.json"


def parse_entry(json_text: str) -> Optional[dict]:
    """registry json 文字 → entry dict；壞檔（非 JSON / 非物件）回 None（不爆）。"""
    try:
        obj = json.loads(json_text)
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    return obj


def read_registry(state_dir) -> dict:
    """掃 <state_dir>/registry/*.json → {name: entry}。

    壞檔 / 不可讀 / 非物件一律跳過（不爆）；name 以 entry 內的 name 為準，
    缺 name 則退回檔名 stem。目錄不存在 → 空 dict。
    """
    rdir = Path(state_dir) / "registry"
    out: dict = {}
    if not rdir.is_dir():
        return out
    for f in sorted(rdir.glob("*.json")):
        try:
            entry = parse_entry(f.read_text())
        except OSError:
            continue
        if entry is None:
            continue
        name = entry.get("name") or f.stem
        out[name] = entry
    return out


def classify(entry: dict, now: int, has_inflight: bool) -> str:
    """liveness 三態分類（純函式）。

    - online：now - last_seen <= LIVENESS_WINDOW（邊界含等於）。
    - processing：超出 window，但該方 inbox 有 in-flight 信（處理期間不心跳）。
    - offline：超出 window 且無 in-flight。
    """
    last_seen = entry.get("last_seen", 0)
    if now - last_seen <= LIVENESS_WINDOW:
        return "online"
    return "processing" if has_inflight else "offline"


def write_entry(state_dir, name: str, mailbox_path: str, cwd: str,
                pid: int, now: int, roles: Optional[List[str]] = None,
                description: str = "") -> Path:
    """落 <name>.json（自建 registry/ 目錄、同名覆寫），回寫入路徑。

    寫入內容可被 read_registry 原樣讀回（roundtrip）。
    roles/description 為能力宣告（缺省 []／""），一併寫進 entry。
    """
    path = registry_path(state_dir, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "name": _normalize_name(name),
        "mailbox_path": str(mailbox_path),
        "cwd": str(cwd),
        "pid": int(pid),
        "last_seen": int(now),
        "roles": list(roles) if roles else [],
        "description": str(description),
    }
    path.write_text(json.dumps(entry, ensure_ascii=False))
    return path


def _read_card(cwd: str) -> str:
    """讀 <cwd>/.mailbox-card 文字；缺檔/不可讀 → ""（best-effort，不爆）。"""
    try:
        return (Path(cwd) / ".mailbox-card").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""


def _cmd_write_self(args) -> int:
    """write-self：解析本方能力（env 優先、否則 .mailbox-card）並寫 registry。

    roles 來源 MAILBOX_ROLES else card；description 來源 MAILBOX_DESC else card。
    best-effort：缺 card → 能力為空，仍寫 entry。
    """
    card = _read_card(args.cwd)
    roles = parse_roles(os.environ.get("MAILBOX_ROLES", ""), card)
    description = parse_description(os.environ.get("MAILBOX_DESC", ""), card)
    write_entry(STATE_DIR, args.name, args.mailbox, args.cwd, args.pid,
                now=int(time.time()), roles=roles, description=description)
    return 0


def _main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="registry")
    sub = parser.add_subparsers(dest="cmd", required=True)
    ws = sub.add_parser(
        "write-self", help="寫/更新本方 registry entry（含自宣告能力）")
    ws.add_argument("--name", required=True)
    ws.add_argument("--mailbox", required=True)
    ws.add_argument("--cwd", required=True)
    ws.add_argument("--pid", required=True, type=int)
    ws.set_defaults(func=_cmd_write_self)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(_main())
