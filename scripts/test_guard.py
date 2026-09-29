#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
safe-delete-guard / 自检

端到端跑 guard.py（模拟 hook 调用：stdin 传 JSON，解析 stdout 决策），
并验证 safe_delete.py 的移入与还原链路。

    python test_guard.py
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
GUARD = HERE / "guard.py"
SAFE_DELETE = HERE / "safe_delete.py"
PY = sys.executable

DENY_CASES = [
    ("rm -rf /home/user/project", "递归删除项目目录"),
    ("rm -rf ~/Desktop", "删除桌面本身"),
    ("rm -rf /", "删除根目录"),
    ("sudo rm -rf /var/www", "sudo 前缀仍应识别命令名"),
    ("rm important.txt", "单个普通文件，全路径策略下也拦截"),
    ("rm -rf $BUILD_DIR", "变量路径无法判定，保守拦截"),
    ("rm -rf --no-preserve-root /", "带 flag 的根删除"),
    ("del /S /Q C:\\Users\\me\\docs", "cmd del 递归"),
    ("Remove-Item -Recurse -Force C:\\Users\\me\\docs", "PowerShell 递归删除"),
    ("git clean -fd", "git clean 删除未跟踪文件"),
    ("git reset --hard", "丢弃工作区改动"),
    ("git checkout -- .", "丢弃全部工作区改动"),
    ("git stash drop", "丢弃 stash"),
    ('python -c "import shutil; shutil.rmtree(\'/x\')"', "Python rmtree"),
    ("find . -name '*.tmp' -delete", "find -delete"),
    ("find . -name '*.tmp' | xargs rm -rf", "管道 xargs 删除"),
    ("ls old_dir | xargs rm", "xargs 删除"),
    ("git rm -rf file.txt", "git rm 删除工作区文件"),
    ("dd if=/dev/zero of=/dev/sda", "dd 写块设备"),
    ("truncate -s 0 data.db", "截断文件"),
    ("node -e \"require('fs').rmSync('x',{recursive:true})\"", "Node fs.rmSync"),
]

ALLOW_CASES = [
    ("rm -rf node_modules", "构建产物白名单"),
    ("rm -rf ./dist", "dist 白名单"),
    ("rm -rf build/output", "多级白名单"),
    ("rm -rf /tmp/build", "临时目录白名单"),
    ("rm debug.log", "日志后缀白名单"),
    ("Remove-Item -Recurse node_modules", "PowerShell 白名单路径"),
    ("npm install", "非删除命令"),
    ("ls -la", "非删除命令"),
    ("git status", "git 只读"),
    ("git clean -n", "git clean dry-run"),
    ("git restore --staged file.txt", "取消暂存不是删除"),
    ("git checkout -b feature", "新建分支不是删除"),
    ("git rm --cached config.env", "只取消跟踪，不删工作区文件"),
    ("dd if=/dev/urandom of=image.bin", "of 非设备，创建文件"),
    ("echo hello > /tmp/brand_new_file_xyz.txt", "重定向到不存在的新文件"),
    ("npm run build", "常规构建命令"),
    ("echo hello > out.txt", "重定向写入"),
]


# 模拟 agent 的真实工作目录。刻意不使用 /tmp：否则相对路径会被补成
# /tmp/xxx 而命中临时目录白名单，掩盖真实判定逻辑。
DEFAULT_CWD = "C:/Users/me/project"


def run_guard(command, tool="Bash", cwd=DEFAULT_CWD):
    payload = {
        "session_id": "test",
        "transcript_path": "/tmp/t.jsonl",
        "cwd": cwd,
        "permission_mode": "default",
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": {"command": command},
    }
    proc = subprocess.run(
        [PY, str(GUARD)],
        input=json.dumps(payload).encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    if proc.returncode != 0:
        return "error", "exit=%s stderr=%s" % (proc.returncode, proc.stderr.decode("utf-8", "replace"))
    try:
        out = json.loads(proc.stdout.decode("utf-8"))
        return out["hookSpecificOutput"]["permissionDecision"], ""
    except Exception as exc:
        return "error", "bad output %r (%s)" % (proc.stdout[:200], exc)


def run_safe_delete(args, env=None):
    full_env = dict(os.environ)
    full_env.update(env or {})
    proc = subprocess.run(
        [PY, str(SAFE_DELETE)] + args + ["--json"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=full_env,
        timeout=60,
    )
    try:
        return json.loads(proc.stdout.decode("utf-8")), proc.returncode
    except Exception:
        return {"ok": False, "raw": proc.stdout.decode("utf-8", "replace"),
                "err": proc.stderr.decode("utf-8", "replace")}, proc.returncode


def main():
    failures = []

    print("=" * 68)
    print("1. 拦截用例（期望 deny）")
    print("=" * 68)
    for cmd, desc in DENY_CASES:
        tool = "PowerShell" if cmd.startswith(("Remove-Item", "Clear-")) else "Bash"
        decision, err = run_guard(cmd, tool)
        ok = decision == "deny"
        print("  [%s] %-52s %s" % ("PASS" if ok else "FAIL", cmd[:52], desc))
        if not ok:
            failures.append(("deny", cmd, decision, err))

    print()
    print("=" * 68)
    print("2. 放行用例（期望 allow）")
    print("=" * 68)
    for cmd, desc in ALLOW_CASES:
        tool = "PowerShell" if cmd.startswith(("Remove-Item", "Clear-")) else "Bash"
        decision, err = run_guard(cmd, tool)
        ok = decision == "allow"
        print("  [%s] %-52s %s" % ("PASS" if ok else "FAIL", cmd[:52], desc))
        if not ok:
            failures.append(("allow", cmd, decision, err))

    print()
    print("=" * 68)
    print("3. 回归用例（已知漏洞防复发）")
    print("=" * 68)
    regression = [
        # cwd 自身名为白名单目录时，不得让该目录下任意删除被豁免
        ("rm -rf src", "C:/Users/me/build", "deny", "cwd 名为 build 时删除 src 必须拦截"),
        ("rm -rf .", "C:/Users/me/dist", "deny", "cwd 名为 dist 时删除当前目录必须拦截"),
        ("rm -rf ./src", "C:/Users/me/node_modules", "deny", "cwd 名为 node_modules 时不得豁免"),
        ("rm -rf node_modules", "C:/Users/me/project", "allow", "正常项目下删 node_modules 仍应放行"),
    ]
    for cmd, cwd, expected, desc in regression:
        decision, err = run_guard(cmd, cwd=cwd)
        ok = decision == expected
        print("  [%s] %-34s (cwd=%s) %s" % ("PASS" if ok else "FAIL", cmd, cwd, desc))
        if not ok:
            failures.append(("regression", "%s @ %s" % (cmd, cwd), decision, err))

    # 重定向截断：目标已存在应拦截
    trunc_cases = 0
    with tempfile.TemporaryDirectory() as td:
        victim = Path(td) / "wipe.txt"
        victim.write_text("keep", encoding="utf-8")
        trunc_cases += 1
        decision, err = run_guard("echo x > wipe.txt", cwd=str(Path(td)))
        ok = decision == "deny"
        print("  [%s] echo x > 已存在文件   (cwd=%s) 截断即删除须拦截" % ("PASS" if ok else "FAIL", str(Path(td))))
        if not ok:
            failures.append(("regression", "redirect truncate", decision, err))

    print()
    print("=" * 68)
    print("4. 安全删除与回滚链路")
    print("=" * 68)
    with tempfile.TemporaryDirectory() as td:
        work = Path(td) / "work"
        work.mkdir()
        victim = work / "keep.txt"
        victim.write_text("important", encoding="utf-8")
        trash = Path(td) / "trash"
        env = {"WB_TRASH_ROOT": str(trash)}

        result, code = run_safe_delete([str(victim)], env)
        moved_ok = result.get("ok") and not victim.exists()
        print("  [%s] 移入回收站后源文件消失" % ("PASS" if moved_ok else "FAIL"))
        if not moved_ok:
            failures.append(("safe_delete.move", str(victim), result, ""))

        entry_id = (result.get("moved") or [{}])[0].get("id")
        result2, _ = run_safe_delete(["--restore", entry_id or "0001"], env)
        restored_ok = result2.get("ok") and victim.exists() and victim.read_text(encoding="utf-8") == "important"
        print("  [%s] 还原后文件与内容完整" % ("PASS" if restored_ok else "FAIL"))
        if not restored_ok:
            failures.append(("safe_delete.restore", entry_id, result2, ""))

        result3, _ = run_safe_delete(["--purge"], env)
        # 根目录会保留 manifest.jsonl，因此只断言日期子目录已清空
        leftover_dirs = [p.name for p in trash.iterdir() if p.is_dir()] if trash.exists() else []
        purge_ok = result3.get("ok") and not leftover_dirs
        print("  [%s] purge 清空回收站" % ("PASS" if purge_ok else "FAIL"))
        if not purge_ok:
            failures.append(("safe_delete.purge", str(leftover_dirs), result3, ""))

    print()
    print("=" * 68)
    if failures:
        print("失败 %d 项：" % len(failures))
        for kind, cmd, got, err in failures:
            print("  - [%s] %s -> 实际 %s %s" % (kind, cmd, got, err))
        return 1
    print("全部通过：%d 拦截 + %d 放行 + %d 回归 + %d 重定向 + 3 项删除/回滚链路"
          % (len(DENY_CASES), len(ALLOW_CASES), len(regression), trunc_cases))
    return 0


if __name__ == "__main__":
    sys.exit(main())
