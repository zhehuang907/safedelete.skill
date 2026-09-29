#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
safe-delete-guard / hook 安装器

把 PreToolUse 拦截 hook 注册到各 AI coding 工具的 settings.json。
支持安装 / 卸载 / 状态检查 / 预演，且始终合并已有 hooks，不覆盖用户配置。

用法：
    python install_hooks.py --status
    python install_hooks.py --dry-run
    python install_hooks.py                       # 安装到 WorkBuddy（默认）
    python install_hooks.py --target all          # 同时安装到 CodeBuddy / Claude Code
    python install_hooks.py --uninstall --target all

生效时机：hooks 在会话启动时加载，安装后需开启新会话（或重启客户端）才生效。
"""

import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

SKILL_DIR = Path(__file__).resolve().parent.parent
GUARD_SCRIPT = SKILL_DIR / "scripts" / "guard.py"

TARGETS = {
    "workbuddy": Path.home() / ".workbuddy" / "settings.json",
    "codebuddy": Path.home() / ".codebuddy" / "settings.json",
    "claude": Path.home() / ".claude" / "settings.json",
}

MATCHER = "Bash|PowerShell"
HOOK_TIMEOUT = 10


def python_executable():
    exe = Path(sys.executable)
    # pythonw.exe 无控制台，hook 场景下拿不到输出，回退到同目录 python.exe
    if exe.name.lower().startswith("pythonw"):
        candidate = exe.with_name(exe.name.replace("pythonw", "python"))
        if candidate.exists():
            return candidate
    return exe


def hook_command():
    exe = python_executable().as_posix()
    script = GUARD_SCRIPT.as_posix()
    return '"%s" "%s"' % (exe, script)


def is_ours(entry):
    """判断某条 hook 是否是本 Skill 注册的（按脚本路径识别，避免重复安装）。

    同时兼容历史目录名 safe-delete-guard，保证从旧版本升级时能正确卸载旧 hook。
    """
    cmd = (entry.get("command", "") or "").replace("\\", "/")
    if GUARD_SCRIPT.as_posix() in cmd:
        return True
    legacy_markers = ("safe-delete-guard", "safedelete")
    return "guard.py" in cmd and any(marker in cmd for marker in legacy_markers)


def load_settings(path):
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError as exc:
        raise SystemExit("解析失败，已中止以免损坏配置：%s (%s)" % (path, exc))


def save_settings(path, data, dry_run=False):
    if dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_suffix(path.suffix + ".wbguard.bak")
        shutil.copy2(path, backup)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def install(path, dry_run=False):
    data = load_settings(path)
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise SystemExit("%s 的 hooks 字段不是对象，已中止" % path)

    pre = hooks.setdefault("PreToolUse", [])
    if not isinstance(pre, list):
        raise SystemExit("%s 的 hooks.PreToolUse 不是数组，已中止" % path)

    # 找到属于本 Skill 的 matcher 分组
    group = None
    for item in pre:
        if isinstance(item, dict) and item.get("matcher") == MATCHER:
            group = item
            break
    if group is None:
        group = {"matcher": MATCHER, "hooks": []}
        pre.append(group)

    group.setdefault("hooks", [])
    existing = [h for h in group["hooks"] if isinstance(h, dict) and is_ours(h)]
    if existing:
        for hook in existing:
            hook["command"] = hook_command()
            hook["type"] = "command"
            hook["timeout"] = HOOK_TIMEOUT
        action = "更新"
    else:
        group["hooks"].append(
            {"type": "command", "command": hook_command(), "timeout": HOOK_TIMEOUT}
        )
        action = "安装"

    save_settings(path, data, dry_run)
    return action


def uninstall(path, dry_run=False):
    if not path.exists():
        return "未安装"
    data = load_settings(path)
    hooks = data.get("hooks", {})
    pre = hooks.get("PreToolUse", [])
    removed = False
    for item in pre:
        if not isinstance(item, dict):
            continue
        before = len(item.get("hooks", []))
        item["hooks"] = [h for h in item.get("hooks", []) if not (isinstance(h, dict) and is_ours(h))]
        if len(item["hooks"]) != before:
            removed = True
    # 清理空分组
    hooks["PreToolUse"] = [i for i in pre if i.get("hooks")]
    if not hooks["PreToolUse"]:
        hooks.pop("PreToolUse", None)
    if not hooks:
        data.pop("hooks", None)
    if removed:
        save_settings(path, data, dry_run)
        return "已卸载"
    return "未安装"


def status(path):
    if not path.exists():
        return "配置文件不存在（未安装）"
    data = load_settings(path)
    pre = (data.get("hooks") or {}).get("PreToolUse", [])
    for item in pre:
        if not isinstance(item, dict):
            continue
        for hook in item.get("hooks", []):
            if isinstance(hook, dict) and is_ours(hook):
                return "已安装 (matcher=%s)" % item.get("matcher")
    return "未安装"


def main():
    parser = argparse.ArgumentParser(description="safe-delete-guard hook 安装器")
    parser.add_argument("--target", default="workbuddy",
                        choices=sorted(TARGETS.keys()) + ["all"],
                        help="目标配置，默认 workbuddy")
    parser.add_argument("--uninstall", action="store_true", help="卸载")
    parser.add_argument("--status", action="store_true", help="查看安装状态")
    parser.add_argument("--dry-run", action="store_true", help="只预览，不写文件")
    args = parser.parse_args()

    names = sorted(TARGETS.keys()) if args.target == "all" else [args.target]

    print("拦截脚本: %s" % GUARD_SCRIPT)
    print("解释器  : %s" % python_executable())
    print("匹配工具: %s\n" % MATCHER)

    for name in names:
        path = TARGETS[name]
        if args.status:
            print("[%s] %s -> %s" % (name, path, status(path)))
            continue
        if args.uninstall:
            print("[%s] %s -> %s" % (name, path, uninstall(path, args.dry_run)))
        else:
            print("[%s] %s -> %s" % (name, path, install(path, args.dry_run)))

    if not args.status and not args.dry_run:
        print("\n完成。hooks 在会话启动时加载，请开启新会话后生效。")
        print("验证方式：在新会话中尝试 `rm -rf <任意非白名单路径>`，应被拦截并给出原因。")
    elif args.dry_run:
        print("\n（预演模式，未写入任何文件）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
