#!/usr/bin/env python3
"""Idempotently wire the poller-relaunch Stop hook into ~/.claude/settings.json.

Safe to run repeatedly: it backs up settings.json, then adds the Stop hook only
if our exact command isn't already present. Matches the standard hook structure
(hooks.<Event> = [ { "hooks": [ {type, command} ] } ]).

The command path is derived from this script's location, so it works wherever you
cloned the repo. Run it yourself (global config is yours to change):

    python3 hooks/install_stop_hook.py
"""
import json
import os
import shutil
import time

SETTINGS = os.path.expanduser("~/.claude/settings.json")
HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stop_relaunch_poller.py")
CMD = "python3 %s" % HOOK


def main():
    d = json.load(open(SETTINGS))
    bak = "%s.bak-%s" % (SETTINGS, time.strftime("%Y%m%d-%H%M%S"))
    shutil.copy(SETTINGS, bak)
    print("backup ->", bak)

    hooks = d.setdefault("hooks", {})
    stop = hooks.setdefault("Stop", [])
    already = any(
        h.get("command") == CMD
        for grp in stop for h in grp.get("hooks", [])
    )
    if already:
        print("Stop hook already present — no change.")
        return
    stop.append({"hooks": [{"type": "command", "command": CMD}]})
    with open(SETTINGS, "w") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print("added Stop hook ->", CMD)


if __name__ == "__main__":
    main()
