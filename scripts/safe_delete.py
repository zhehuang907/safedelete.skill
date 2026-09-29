#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
safe-delete-guard / 安全删除与回滚

把目标移入回收站（默认 ~/.workbuddy/trash），而不是直接删除。
所有条目记录在 manifest.jsonl 中，可随时还原。

用法：
    python safe_delete.py <路径> [更多路径...]     移入回收站
    python safe_delete.py --list                    列出可还原条目
    python safe_delete.py --restore <id|all>        还原（id 可用前缀）
    python safe_delete.py --purge [--older-than 30d] 永久清空
    python safe_delete.py --json ...                以 JSON 输出结果

设计约定：
    * 与 guard.py 共用同一回收站根目录（环境变量 WB_TRASH_ROOT 可覆盖）。
    * 移动失败时不留半成品：先复制再删源，任一步失败即回滚。
    * 回收站内的条目不会永久增长，用 --purge 按时间清理。
"""

import argparse
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

try:  # Windows 控制台默认可能是 GBK，强制 UTF-8 输出
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


def trash_root():
    return Path(os.environ.get("WB_TRASH_ROOT") or (Path.home() / ".workbuddy" / "trash"))


MANIFEST_NAME = "manifest.jsonl"


# --------------------------------------------------------------------------
# manifest 读写
# --------------------------------------------------------------------------
def read_manifest(root):
    path = root / MANIFEST_NAME
    if not path.exists():
        return []
    entries = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def write_manifest(root, entries):
    root.mkdir(parents=True, exist_ok=True)
    path = root / MANIFEST_NAME
    with open(path, "w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def next_id(entries):
    used = {e["id"] for e in entries}
    n = 1
    while True:
        candidate = "%04d" % n
        if candidate not in used:
            return candidate
        n += 1


# --------------------------------------------------------------------------
# 删除（移入回收站）
# --------------------------------------------------------------------------
def move_to_trash(source, root):
    """把单个路径移入回收站，返回 manifest 条目；失败时抛异常。"""
    src = Path(os.path.abspath(os.path.expanduser(str(source))))
    if not src.exists():
        raise FileNotFoundError("路径不存在: %s" % src)

    entries = read_manifest(root)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest_dir = root / stamp
    dest_dir.mkdir(parents=True, exist_ok=True)

    dest = dest_dir / src.name
    if dest.exists():  # 同名冲突加序号
        idx = 1
        while dest.exists():
            dest = dest_dir / ("%s (%d)" % (src.name, idx))
            idx += 1

    try:
        # 同盘 rename（原子且快）；跨盘时 shutil.move 退化为复制 + 删源
        shutil.move(str(src), str(dest))
        entry = {
            "id": next_id(entries),
            "ts": datetime.now().isoformat(timespec="seconds"),
            "original": str(src),
            "trash": str(dest),
            "kind": "dir" if dest.is_dir() else "file",
        }
        entries.append(entry)
        write_manifest(root, entries)
        return entry
    except Exception:
        # 尽力回滚：若源已消失但目标已存在，把目标挪回去
        if not src.exists() and dest.exists():
            try:
                shutil.move(str(dest), str(src))
            except Exception:
                pass
        raise


# --------------------------------------------------------------------------
# 还原 / 清理
# --------------------------------------------------------------------------
def restore(entry_id, root):
    entries = read_manifest(root)
    targets = [e for e in entries if e["id"] == entry_id or e["id"].startswith(entry_id)]
    if entry_id != "all" and len(targets) != 1:
        raise ValueError("ID `%s` 匹配到 %d 条记录，请用 --list 确认" % (entry_id, len(targets)))
    if entry_id == "all":
        targets = entries

    restored = []
    remaining = []
    for entry in entries:
        if entry not in targets:
            remaining.append(entry)
            continue
        original = Path(entry["original"])
        trash_path = Path(entry["trash"])
        if not trash_path.exists():
            remaining.append(entry)  # 已被清理，保留记录由 --purge 处理
            continue
        original.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(trash_path), str(original))
        restored.append(entry)

    write_manifest(root, remaining)
    return restored


def purge(root, older_than=None):
    entries = read_manifest(root)
    cutoff = None
    if older_than:
        match = re.match(r"^(\d+)([dhms])$", older_than.strip().lower())
        if not match:
            raise ValueError("--older-than 格式应为 30d / 12h / 45m / 60s")
        amount, unit = int(match.group(1)), match.group(2)
        seconds = {"d": 86400, "h": 3600, "m": 60, "s": 1}[unit]
        cutoff = time.time() - amount * seconds

    kept = []
    removed = []
    for entry in entries:
        if cutoff is not None:
            try:
                ts = datetime.fromisoformat(entry["ts"]).timestamp()
            except Exception:
                ts = time.time()
            if ts > cutoff:
                kept.append(entry)
                continue
        trash_path = Path(entry["trash"])
        if trash_path.exists():
            if trash_path.is_dir():
                shutil.rmtree(trash_path)
            else:
                trash_path.unlink()
        removed.append(entry)

    write_manifest(root, kept)
    # 清掉空的日期目录
    for child in root.iterdir():
        if child.is_dir() and not any(child.iterdir()):
            child.rmdir()
    return removed


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="安全删除：移入回收站并支持回滚")
    parser.add_argument("paths", nargs="*", help="要移入回收站的路径")
    parser.add_argument("--list", action="store_true", help="列出回收站条目")
    parser.add_argument("--restore", metavar="ID", help="还原指定 ID（或 all）")
    parser.add_argument("--purge", action="store_true", help="永久删除回收站内容")
    parser.add_argument("--older-than", metavar="Nd", help="配合 --purge，仅清理早于该时长的条目")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = parser.parse_args()

    root = trash_root()
    result = {"ok": True, "trash_root": str(root)}

    try:
        if args.list:
            result["entries"] = read_manifest(root)
            if not args.json:
                if not result["entries"]:
                    print("回收站为空：%s" % root)
                else:
                    print("%-6s %-20s %-8s %s" % ("ID", "时间", "类型", "原路径"))
                    for e in result["entries"]:
                        print("%-6s %-20s %-8s %s" % (e["id"], e["ts"], e["kind"], e["original"]))

        elif args.restore:
            restored = restore(args.restore, root)
            result["restored"] = restored
            if not args.json:
                print("已还原 %d 项：" % len(restored))
                for e in restored:
                    print("  [%s] -> %s" % (e["id"], e["original"]))

        elif args.purge:
            removed = purge(root, args.older_than)
            result["purged"] = removed
            if not args.json:
                print("已永久删除 %d 项" % len(removed))

        elif args.paths:
            moved = []
            for path in args.paths:
                moved.append(move_to_trash(path, root))
            result["moved"] = moved
            if not args.json:
                for e in moved:
                    print("已移入回收站 [%s]：%s" % (e["id"], e["original"]))
                print("\n如需撤销：python %s --restore %s"
                      % (Path(__file__).resolve(), moved[0]["id"] if moved else "<id>"))

        else:
            parser.print_help()
            return 0

    except Exception as exc:
        result = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        else:
            sys.stderr.write("失败：%s\n" % result["error"])
        return 1

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
