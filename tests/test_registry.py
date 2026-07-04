"""TDD for registry core (純函式 + tmp_path I/O)。

session 註冊表 — 每方一檔 `.state/registry/<name>.json`：
- registry_path(state_dir, name): 該方 json 路徑（name 正規化 [a-z0-9_-]+）
- parse_entry(json_text): JSON 文字 → entry dict（壞檔回 None）
- read_registry(state_dir): 掃 registry/ → {name: entry}（壞檔/不可讀跳過不爆）
- classify(entry, now, has_inflight): online / processing / offline 三態
- write_entry(...): 落 <name>.json，可被 read_registry 讀回
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import registry as reg


# ---------- registry_path（路徑 + name 正規化）----------

def test_registry_path_under_state_registry_dir(tmp_path):
    p = reg.registry_path(tmp_path, "service-a")
    assert p == tmp_path / "registry" / "service-a.json"


def test_registry_path_normalizes_name_to_safe_charset(tmp_path):
    # 大寫 / 空白 / 斜線等不安全字元 → 收斂到 [a-z0-9_-]
    p = reg.registry_path(tmp_path, "Quant Data/v2")
    assert p.name == "quant-data-v2.json"
    # 仍在 registry/ 底下，無路徑穿越
    assert p.parent == tmp_path / "registry"


def test_registry_path_keeps_allowed_chars(tmp_path):
    p = reg.registry_path(tmp_path, "back_test-01")
    assert p.name == "back_test-01.json"


# ---------- parse_entry ----------

_GOOD = ('{"name":"foo","mailbox_path":"/repo/foo/mailbox",'
         '"cwd":"/repo/foo","pid":4242,"last_seen":1700000000}')


def test_parse_entry_returns_full_dict():
    e = reg.parse_entry(_GOOD)
    assert e["name"] == "foo"
    assert e["mailbox_path"] == "/repo/foo/mailbox"
    assert e["cwd"] == "/repo/foo"
    assert e["pid"] == 4242
    assert e["last_seen"] == 1700000000


def test_parse_entry_malformed_json_returns_none():
    assert reg.parse_entry("{not json at all") is None


def test_parse_entry_non_object_returns_none():
    # JSON array / 數字 不是 entry → None（不該當 dict 用）
    assert reg.parse_entry("[1,2,3]") is None
    assert reg.parse_entry("42") is None


# ---------- read_registry ----------

def test_read_registry_collects_entries_by_name(tmp_path):
    rdir = tmp_path / "registry"
    rdir.mkdir(parents=True)
    (rdir / "foo.json").write_text(_GOOD)
    (rdir / "bar.json").write_text(
        '{"name":"bar","mailbox_path":"/r/bar/mailbox","cwd":"/r/bar",'
        '"pid":7,"last_seen":1700000100}')
    out = reg.read_registry(tmp_path)
    assert set(out) == {"foo", "bar"}
    assert out["bar"]["mailbox_path"] == "/r/bar/mailbox"


def test_read_registry_skips_garbage_files(tmp_path):
    rdir = tmp_path / "registry"
    rdir.mkdir(parents=True)
    (rdir / "good.json").write_text(_GOOD.replace('"foo"', '"good"'))
    (rdir / "broken.json").write_text("{ this is not json")
    (rdir / "notjson.txt").write_text("ignored")  # 非 .json 一律不看
    out = reg.read_registry(tmp_path)
    assert set(out) == {"good"}  # 壞檔跳過、非 json 忽略，不爆


def test_read_registry_missing_dir_returns_empty(tmp_path):
    # registry/ 不存在 → 空 dict，不爆
    assert reg.read_registry(tmp_path) == {}


# ---------- classify（liveness 三態）----------

def test_classify_fresh_heartbeat_is_online():
    entry = {"last_seen": 1000}
    now = 1000 + 30  # 30s ago，window 內
    assert reg.classify(entry, now, has_inflight=False) == "online"


def test_classify_window_boundary_is_online():
    # 邊界：剛好等於 window → 仍 online（<= window）
    entry = {"last_seen": 1000}
    now = 1000 + reg.LIVENESS_WINDOW
    assert reg.classify(entry, now, has_inflight=False) == "online"


def test_classify_stale_with_inflight_is_processing():
    entry = {"last_seen": 1000}
    now = 1000 + reg.LIVENESS_WINDOW + 1  # 超出 window
    assert reg.classify(entry, now, has_inflight=True) == "processing"


def test_classify_stale_without_inflight_is_offline():
    entry = {"last_seen": 1000}
    now = 1000 + 600  # 10 分鐘前，window 外
    assert reg.classify(entry, now, has_inflight=False) == "offline"


def test_liveness_window_default_is_180():
    assert reg.LIVENESS_WINDOW == 180


# ---------- write_entry（落檔 → read_registry 讀回）----------

def test_write_entry_roundtrips_through_read_registry(tmp_path):
    reg.write_entry(tmp_path, "backtest", "/r/backtest/mailbox",
                    "/r/backtest", 99, 1700000200)
    out = reg.read_registry(tmp_path)
    assert "backtest" in out
    e = out["backtest"]
    assert e["name"] == "backtest"
    assert e["mailbox_path"] == "/r/backtest/mailbox"
    assert e["cwd"] == "/r/backtest"
    assert e["pid"] == 99
    assert e["last_seen"] == 1700000200


def test_write_entry_creates_registry_dir(tmp_path):
    # registry/ 尚不存在時，write_entry 應自建
    assert not (tmp_path / "registry").exists()
    reg.write_entry(tmp_path, "foo", "/m", "/c", 1, 5)
    assert reg.registry_path(tmp_path, "foo").exists()


def test_write_entry_overwrites_same_name(tmp_path):
    # 同名重開 → 覆寫（last_seen 更新）
    reg.write_entry(tmp_path, "foo", "/m", "/c", 1, 100)
    reg.write_entry(tmp_path, "foo", "/m", "/c", 2, 200)
    out = reg.read_registry(tmp_path)
    assert out["foo"]["pid"] == 2
    assert out["foo"]["last_seen"] == 200


# ---------- parse_roles（能力宣告：roles）----------

def test_parse_roles_env_wins_over_card():
    # env_val 非空 → 用 env，忽略 card
    card = "roles: from-card\n"
    assert reg.parse_roles("frontend", card) == ["frontend"]


def test_parse_roles_env_comma_separated():
    # env 逗號分隔 → 多 role
    assert reg.parse_roles("a, b", "") == ["a", "b"]


def test_parse_roles_from_card_roles_line():
    card = "roles: data-ingest, backfill\ndesc: 採集\n"
    assert reg.parse_roles("", card) == ["data-ingest", "backfill"]


def test_parse_roles_card_ignores_hash_comment_lines():
    # '#' 開頭整行為註解 → 忽略
    card = "# roles: should-be-ignored\nroles: real\n"
    assert reg.parse_roles("", card) == ["real"]


def test_parse_roles_normalizes_each_role():
    # 每個 role 正規化 [a-z0-9_-]（同 name）
    assert reg.parse_roles("Data Ingest, Back/Test", "") == [
        "data-ingest", "back-test"]


def test_parse_roles_drops_empties_and_strips():
    # 空白片段（連續逗號、結尾逗號）丟掉、修邊
    assert reg.parse_roles("a, , b,", "") == ["a", "b"]


def test_parse_roles_dedup_preserving_order():
    assert reg.parse_roles("a, b, a, c, b", "") == ["a", "b", "c"]


def test_parse_roles_empty_env_and_no_card_line_returns_empty():
    assert reg.parse_roles("", "") == []
    assert reg.parse_roles("", "desc: only desc here\n") == []


def test_parse_roles_empty_env_none_card_returns_empty():
    # 缺省（env 空、card None/空）→ []
    assert reg.parse_roles("", None) == []


# ---------- parse_description（能力宣告：description）----------

def test_parse_description_env_wins_over_card():
    card = "desc: from card\n"
    assert reg.parse_description("from env", card) == "from env"


def test_parse_description_from_card_desc_line():
    card = "roles: a\ndesc: 負責 FinMind 採集\n"
    assert reg.parse_description("", card) == "負責 FinMind 採集"


def test_parse_description_empty_returns_empty_string():
    assert reg.parse_description("", "") == ""
    assert reg.parse_description("", "roles: a\n") == ""
    assert reg.parse_description("", None) == ""


def test_parse_description_truncates_to_200_chars():
    long = "x" * 500
    out = reg.parse_description(long, "")
    assert len(out) == 200
    assert out == "x" * 200


def test_parse_description_is_single_line():
    # 取行值；不應含換行
    out = reg.parse_description("", "desc: one line value\nroles: a\n")
    assert out == "one line value"
    assert "\n" not in out


# ---------- write_entry + read_registry 保留 roles / description ----------

def test_write_entry_roundtrips_roles_and_description(tmp_path):
    reg.write_entry(tmp_path, "ingest", "/m", "/c", 7, 1700000000,
                    roles=["data-ingest", "backfill"],
                    description="負責採集")
    out = reg.read_registry(tmp_path)
    e = out["ingest"]
    assert e["roles"] == ["data-ingest", "backfill"]
    assert e["description"] == "負責採集"


def test_write_entry_defaults_roles_empty_and_description_blank(tmp_path):
    # 沒傳 roles/description → 缺省 []／""（仍寫進 entry）
    reg.write_entry(tmp_path, "plain", "/m", "/c", 1, 5)
    e = reg.read_registry(tmp_path)["plain"]
    assert e["roles"] == []
    assert e["description"] == ""


def test_read_registry_old_entry_without_capability_fields(tmp_path):
    # 舊 entry（無 roles/description 欄位）讀回不爆，欄位就是沒有
    rdir = tmp_path / "registry"
    rdir.mkdir(parents=True)
    (rdir / "old.json").write_text(
        '{"name":"old","mailbox_path":"/m","cwd":"/c",'
        '"pid":3,"last_seen":1700000000}')
    out = reg.read_registry(tmp_path)
    assert "old" in out
    e = out["old"]
    assert e["name"] == "old"
    # 缺欄位 → 讀回時就是不存在（呼叫端自行 .get 預設），不該爆
    assert "roles" not in e
    assert "description" not in e


# ---------- write-self CLI（argparse __main__）----------

import os
import subprocess

_REG_PY = str(Path(__file__).resolve().parent.parent / "registry.py")


def _run_write_self(args, env_extra=None, cwd=None):
    env = dict(os.environ)
    # 確保不被外部 env 干擾
    env.pop("MAILBOX_ROLES", None)
    env.pop("MAILBOX_DESC", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, _REG_PY, "write-self", *args],
        env=env, cwd=cwd, capture_output=True, text=True)


def test_write_self_cli_reads_roles_from_env(tmp_path, monkeypatch):
    # STATE_DIR 用 env 指 tmp，避免污染真實 .state
    monkeypatch.setenv("MAILBOX_ROLES", "frontend, ui")
    proc = subprocess.run(
        [sys.executable, _REG_PY, "write-self",
         "--name", "fe", "--mailbox", "/m", "--cwd", str(tmp_path),
         "--pid", "123"],
        env={**os.environ, "MAILBOX_STATE_DIR": str(tmp_path)},
        capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    e = reg.read_registry(tmp_path)["fe"]
    assert e["roles"] == ["frontend", "ui"]
    assert e["pid"] == 123
    assert e["mailbox_path"] == "/m"


def test_write_self_cli_reads_card_when_no_env(tmp_path):
    cwd = tmp_path / "repo"
    cwd.mkdir()
    (cwd / ".mailbox-card").write_text(
        "roles: data-ingest, backfill\ndesc: 負責採集\n")
    proc = subprocess.run(
        [sys.executable, _REG_PY, "write-self",
         "--name", "ingest", "--mailbox", "/m", "--cwd", str(cwd),
         "--pid", "9"],
        env={k: v for k, v in os.environ.items()
             if k not in ("MAILBOX_ROLES", "MAILBOX_DESC")}
        | {"MAILBOX_STATE_DIR": str(tmp_path)},
        capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    e = reg.read_registry(tmp_path)["ingest"]
    assert e["roles"] == ["data-ingest", "backfill"]
    assert e["description"] == "負責採集"


def test_write_self_cli_missing_card_yields_empty(tmp_path):
    cwd = tmp_path / "repo"
    cwd.mkdir()  # 無 .mailbox-card
    proc = subprocess.run(
        [sys.executable, _REG_PY, "write-self",
         "--name", "bare", "--mailbox", "/m", "--cwd", str(cwd),
         "--pid", "1"],
        env={k: v for k, v in os.environ.items()
             if k not in ("MAILBOX_ROLES", "MAILBOX_DESC")}
        | {"MAILBOX_STATE_DIR": str(tmp_path)},
        capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    e = reg.read_registry(tmp_path)["bare"]
    assert e["roles"] == []
    assert e["description"] == ""


# ---------- cmux surface 自報（supervisor-script-wake） ----------

def test_write_entry_with_surface_records_field(tmp_path):
    # --surface 為 cmux 別名 → 記成 wake:{cmux, <surface>}
    p = reg.write_entry(tmp_path, "x", "/mb", "/cwd", 1, now=10,
                             surface="9C447FDD-ACB4-4794-AACC-9477AA3D8060")
    import json
    e = json.loads(p.read_text())
    assert e["wake"] == {"backend": "cmux", "target": "9C447FDD-ACB4-4794-AACC-9477AA3D8060"}


def test_write_entry_without_surface_omits_field(tmp_path):
    p = reg.write_entry(tmp_path, "x", "/mb", "/cwd", 1, now=10)
    import json
    assert "cmux_surface" not in json.loads(p.read_text())


def test_write_entry_empty_surface_preserves_existing(tmp_path):
    # launchd 生的 detached poller 沒 cmux context → identify 空；心跳不得擦掉
    # session 先前自報的 surface（supervisor 的注入目標）。
    reg.write_entry(tmp_path, "x", "/mb", "/cwd", 1, now=10, surface="KEEP-ME")
    reg.write_entry(tmp_path, "x", "/mb", "/cwd", 2, now=20)   # 無 target 的心跳
    import json
    e = json.loads((tmp_path / "registry" / "x.json").read_text())
    assert e["wake"] == {"backend": "cmux", "target": "KEEP-ME"}


# ---------- host-aware wake target（tmux/cmux backend）----------

def test_write_entry_records_wake_tmux(tmp_path):
    p = reg.write_entry(tmp_path, "agy", "/mb", "/cwd", 1, now=10,
                        wake_backend="tmux", wake_target="agy-abc")
    import json
    e = json.loads(p.read_text())
    assert e["wake"] == {"backend": "tmux", "target": "agy-abc"}

def test_write_entry_records_wake_cmux(tmp_path):
    p = reg.write_entry(tmp_path, "x", "/mb", "/cwd", 1, now=10,
                        wake_backend="cmux", wake_target="SURF")
    import json
    assert json.loads(p.read_text())["wake"] == {"backend": "cmux", "target": "SURF"}

def test_write_entry_surface_alias_maps_to_cmux_wake(tmp_path):
    # 舊 --surface 相容：等同 cmux backend
    p = reg.write_entry(tmp_path, "x", "/mb", "/cwd", 1, now=10, surface="SURF")
    import json
    e = json.loads(p.read_text())
    assert e.get("wake") == {"backend": "cmux", "target": "SURF"} or e.get("cmux_surface") == "SURF"

def test_write_entry_no_wake_when_none(tmp_path):
    p = reg.write_entry(tmp_path, "x", "/mb", "/cwd", 1, now=10)
    import json
    assert "wake" not in json.loads(p.read_text())
