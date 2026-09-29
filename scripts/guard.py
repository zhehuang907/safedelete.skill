#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
safe-delete-guard / PreToolUse hook

在 Bash / PowerShell 工具执行前拦截破坏性删除命令。
通过 stdin 接收 hook payload(JSON)，向 stdout 输出 hook 决策 JSON。

判定顺序：
  1. 非目标工具 / 取不到命令文本      -> allow
  2. 命令不匹配任何危险规则            -> allow
  3. 路径解析失败或命令中无明确路径     -> deny  (无法判定即保守拦截)
  4. 任一路径命中 protected_exact_paths -> deny
  5. 所有路径命中白名单                -> allow
  6. 其余                              -> deny

失败策略：脚本内部异常时按 on_error 配置处理（默认 allow，保证不锁死终端），
异常详情写入日志。
"""

import json
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(__file__).resolve().parent / "guard_config.json"


def trash_root():
    """回收站根目录。safe_delete.py 使用同一实现，保证两边一致。"""
    return os.environ.get("WB_TRASH_ROOT") or str(Path.home() / ".workbuddy" / "trash")

# 顶层分隔符：用于把复合命令拆成若干段
_SEGMENT_SPLIT_RE = re.compile(r"\n|&&|\|\||(?<!\|)\|(?!\|)|;")
# 环境变量赋值前缀：FOO=bar cmd -> cmd
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=\S*$")
_QUOTES = ("'", '"')

# 廉价预检：命令不含任何危险关键词时直接放行，跳过全量正则，避免每个命令都付出开销。
# 注意 `\bnode\b` 不会误中 `node_modules`（_ 是词内字符，无词边界）。
_QUICK_RE = re.compile(
    r"\b(?:rm|rmdir|del|erase|unlink|shred|truncate|remove-item|clear-content|"
    r"clear-recyclebin|format-volume|git|python|python3|node|php|perl|ruby|dd|"
    r"xargs|find|rsync|shutil|rmtree|rimraf|mv)\b|"
    r"\bfs\.(?:rm|unlink|rmSync)|fs-extra|>|"
)


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------
def log_event(config, payload, decision, detail):
    """把拦截/异常事件写入 JSONL 日志。任何日志失败都不允许影响主流程。"""
    if not config.get("log_enabled", True):
        return
    try:
        log_file = Path(os.path.expanduser(config.get("log_file", "guard.log")))
        if not log_file.is_absolute():
            log_file = SKILL_DIR / log_file
        log_file.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "decision": decision,
            "tool": payload.get("tool_name"),
            "command": (payload.get("tool_input") or {}).get("command", "")[:2000],
            "cwd": payload.get("cwd", ""),
            "detail": detail,
        }
        with open(log_file, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def emit(payload_obj):
    """用二进制写 stdout，规避 Windows 默认编码不是 UTF-8 的问题。"""
    data = json.dumps(payload_obj, ensure_ascii=False).encode("utf-8")
    try:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
    except AttributeError:  # pragma: no cover - 非标准 stdout
        sys.stdout.write(data.decode("utf-8"))
        sys.stdout.flush()


def allow():
    emit({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}})
    sys.exit(0)


def deny(reason):
    emit(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
    )
    sys.exit(0)


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
        config = json.load(fh)

    # 动态补充当前机器的临时目录与回收站根目录到白名单前缀
    # （避免把用户名等机器相关信息写死在配置文件里）
    prefixes = config.setdefault("whitelist_path_prefixes", [])
    dynamic = [tempfile.gettempdir(), os.environ.get("TEMP"), os.environ.get("TMP"), trash_root()]
    for candidate in dynamic:
        if not candidate:
            continue
        normalized = str(candidate).replace("\\", "/").rstrip("/") + "/"
        if normalized not in prefixes:
            prefixes.append(normalized)
    return config


# --------------------------------------------------------------------------
# 命令切分与解析
# --------------------------------------------------------------------------
def split_segments(command):
    """按顶层分隔符切分命令，忽略引号内的分隔符。"""
    segments = []
    buf = []
    quote = None
    i = 0
    text = command or ""
    while i < len(text):
        ch = text[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in _QUOTES:
            quote = ch
            buf.append(ch)
            i += 1
            continue
        match = _SEGMENT_SPLIT_RE.match(text, i)
        if match and match.start() == i:
            segments.append("".join(buf))
            buf = []
            i = match.end()
            continue
        buf.append(ch)
        i += 1
    segments.append("".join(buf))
    return [seg.strip() for seg in segments if seg.strip()]


def tokenize(segment):
    """把一段命令切成 token，保留引号内的空格。"""
    tokens = []
    buf = []
    quote = None
    for ch in segment:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in _QUOTES:
            quote = ch
            buf.append(ch)
            continue
        if ch.isspace():
            if buf:
                tokens.append("".join(buf))
                buf = []
            continue
        buf.append(ch)
    if buf:
        tokens.append("".join(buf))
    return [tok.strip(_QUOTES[0] + _QUOTES[1]) for tok in tokens if tok.strip()]


def command_name(tokens):
    """跳过 env 赋值与 sudo/env 前缀，返回 (命令名小写, 命令名所在下标)。

    返回下标是为了让 extract_paths 只从命令名之后开始收路径，
    否则 `sudo rm -rf x` 会把 `rm` 自身当成路径。
    """
    idx = 0
    while idx < len(tokens) and _ENV_ASSIGN_RE.match(tokens[idx]):
        idx += 1
    if idx >= len(tokens):
        return "", -1
    raw = tokens[idx]
    base = raw.replace("\\", "/").rsplit("/", 1)[-1]

    # 处理 sudo rm / env rm / /usr/bin/rm 形式
    if base.lower() in ("sudo", "env", "command") and idx + 1 < len(tokens):
        idx += 1
        base = tokens[idx].replace("\\", "/").rsplit("/", 1)[-1]
    return base.lower(), idx


def detect_shell(tokens):
    """粗略判定语义方言：powershell / cmd / bash。仅用于规则筛选，误判不影响安全面。"""
    joined = " ".join(tokens)
    if re.search(r"\b(Remove-Item|Clear-Content|Clear-RecycleBin|Format-Volume)\b", joined, re.I):
        return "powershell"
    if re.search(r"(^|\s)/(s|q|f)\b", joined, re.I):
        return "cmd"
    return "bash"


# --------------------------------------------------------------------------
# 路径提取与白名单判定
# --------------------------------------------------------------------------
def extract_paths(tokens, start_index):
    """从命令名之后开始提取路径 token：跳过 flag（-- 之后全收）。"""
    paths = []
    seen_double_dash = False
    for idx in range(max(start_index, 0) + 1, len(tokens)):
        tok = tokens[idx]
        if tok == "--":
            seen_double_dash = True
            continue
        if not seen_double_dash and tok.startswith("-"):
            continue
        if tok in ("&&", "||", "|", ";"):
            continue
        paths.append(tok)
    return paths


def normalize_path(path, cwd):
    """把路径整理成便于匹配的形式：正斜杠、展开 ~、相对路径补 cwd。"""
    text = path.replace("\\", "/").strip().strip("'\"")
    if not text:
        return ""
    if text.startswith("~"):
        text = os.path.expanduser(text)
    if not re.match(r"^[A-Za-z]:/", text) and not text.startswith("/"):
        text = os.path.join(cwd or ".", text)
    return text.replace("\\", "/").rstrip("/")


def is_protected(norm, protected):
    """命中受保护根路径（如 ~、C:/）本身 -> True。"""
    target = norm.rstrip("/") or "/"
    for item in protected:
        item_norm = os.path.expanduser(item).replace("\\", "/").rstrip("/")
        if not item_norm:
            continue
        if target == item_norm or target == item_norm + "/":
            return True
    return False


def relative_part(norm, cwd):
    """剔除 cwd 前缀，只保留相对部分。

    必须这么做：若 cwd 本身就是 build/ / dist/ 这类白名单目录名，
    直接对绝对路径做组件匹配会导致该目录下任何删除都被误判为安全。
    """
    cwd_norm = (cwd or "").replace("\\", "/").rstrip("/")
    if cwd_norm and norm.lower().startswith(cwd_norm.lower() + "/"):
        return norm[len(cwd_norm) + 1:]
    return norm


def is_whitelisted(norm, config, cwd):
    """路径是否属于构建产物 / 缓存 / 临时目录。

    组件与后缀判定基于「剔除 cwd 后的相对部分」；
    前缀判定基于完整绝对路径（例如系统临时目录）。
    """
    lowered = norm.lower()

    for prefix in config.get("whitelist_path_prefixes", []):
        prefix_norm = os.path.expanduser(prefix).replace("\\", "/").rstrip("/") + "/"
        if lowered.startswith(prefix_norm.lower()):
            return True

    rel = relative_part(norm, cwd).lower()

    for suffix in config.get("whitelist_suffixes", []):
        if rel.endswith(suffix.lower()):
            return True

    components = [c for c in rel.split("/") if c]
    allowed = {c.lower() for c in config.get("whitelist_dir_components", [])}
    for comp in components:
        if comp in allowed:
            return True
    return False


def is_absolute(path):
    return bool(re.match(r"^[A-Za-z]:/", path)) or path.startswith("/") or path.startswith("\\\\")


def is_whitelisted_strict(norm, config):
    """仅用于 cwd 之外的绝对路径：只放行前缀白名单（系统临时目录）和后缀白名单。

    不放行目录组件白名单，避免 `/home/me/build/src` 这类「项目根恰好叫 build」的
    跨项目路径被误判为安全。普通开发清理绝大多数发生在 cwd 之内，不受此限制影响。
    """
    lowered = norm.lower()
    for prefix in config.get("whitelist_path_prefixes", []):
        prefix_norm = os.path.expanduser(prefix).replace("\\", "/").rstrip("/") + "/"
        if lowered.startswith(prefix_norm.lower()):
            return True
    for suffix in config.get("whitelist_suffixes", []):
        if lowered.endswith(suffix.lower()):
            return True
    return False


def check_redirect_truncate(command, cwd):
    """检测 `> file`（非 >>、非 /dev/null、非句柄重定向）且目标文件已存在。

    这类重定向会清空已存在文件的内容，等效于删除，但 `echo x > a.log` 写新文件是正常操作，
    因此只对「目标已存在」的情况拦截，避免误伤常规输出重定向。
    """
    try:
        for m in re.finditer(r"(?<![>])(?:(?:1|2|&)?>)(?!>)\s*([^\s|&;'\"\n]+)", command):
            target = m.group(1).strip().strip("'\"")
            if target in ("/dev/null", "nul", "2>&1", "1>&2", "&2", "2>&1"):
                continue
            norm = normalize_path(target, cwd)
            if norm and os.path.exists(norm):
                return norm
    except Exception:
        pass
    return None


# --------------------------------------------------------------------------
# 主判定
# --------------------------------------------------------------------------
def match_danger_command(tokens, cmd_name, shell, config):
    """返回命中的危险规则描述，未命中返回 None。"""
    full = " ".join(tokens)
    for rule in config.get("danger_commands", []):
        names = [n.lower() for n in rule.get("names", [])]
        if cmd_name not in names:
            continue
        rule_shell = rule.get("shell")
        if rule_shell and rule_shell != shell:
            continue
        required = rule.get("require_flags", [])
        if required and not any(flag in full for flag in required):
            continue
        return rule
    return None


def match_danger_pattern(command, config):
    for rule in config.get("danger_patterns", []):
        try:
            if re.search(rule["pattern"], command):
                return rule
        except re.error:
            continue
    return None


def evaluate(config, tool_name, command, cwd):
    """返回 (decision, reason)；decision 取值 allow / deny。"""
    field = config.get("tool_command_field", {}).get(tool_name)
    if not field or not command:
        return "allow", "no command payload"

    # 廉价预检：无危险关键词直接放行，绝大多数正常命令（npm/ls/echo/git status…）走这里
    if not _QUICK_RE.search(command.lower()):
        return "allow", "no destructive keyword matched"

    # 1.5) 重定向截断：`> file` 且目标已存在，等效于删除
    truncated = check_redirect_truncate(command, cwd)
    if truncated:
        return "deny", (
            "[safedelete] 已拦截：重定向 `> %s` 会清空已存在文件的内容（等效删除）。\n"
            "如确需覆盖，请先确认原文件已备份；常规日志输出请改用可回滚方式：\n"
            "  python %s/scripts/safe_delete.py %s   # 先入回收站再处理"
            % (truncated, SKILL_DIR, truncated)
        )

    # 1) 正则类规则（git / 语言 API）：命中即拦，不做路径白名单豁免
    pattern_rule = match_danger_pattern(command, config)
    if pattern_rule:
        return "deny", (
            "[safedelete] 已拦截：%s。\n"
            "该操作会丢弃工作区内容且不可撤销。\n"
            "如需执行，请先确认改动已备份（git stash / 手动备份），再改用安全方式：\n"
            "  python %s/scripts/safe_delete.py <路径>   # 移入回收站，可回滚"
            % (pattern_rule["desc"], SKILL_DIR)
        )

    # 2) 命令类规则
    for segment in split_segments(command):
        tokens = tokenize(segment)
        if not tokens:
            continue
        cmd_name, cmd_idx = command_name(tokens)
        shell = detect_shell(tokens)
        rule = match_danger_command(tokens, cmd_name, shell, config)
        if not rule:
            continue

        if rule.get("always_deny"):
            # 影响范围无法静态判定（find -delete / rsync --delete / truncate 等），
            # 不做白名单豁免，一律拦截。
            return "deny", (
                "[safedelete] 已拦截：%s。\n"
                "该命令的影响范围无法静态判定，因此不应用白名单豁免。\n"
                "如确需执行，请改用可回滚的安全删除：\n"
                "  python %s/scripts/safe_delete.py <路径>"
                % (rule["desc"], SKILL_DIR)
            )

        paths = extract_paths(tokens, cmd_idx)
        if not paths:
            return "deny", (
                "[safedelete] 已拦截：%s，但命令中没有可判定的明确路径（可能使用了变量或通配）。\n"
                "无法确认影响范围，因此保守拦截。\n"
                "请改写为明确路径后重试，或改用安全删除：\n"
                "  python %s/scripts/safe_delete.py <路径>"
                % (rule["desc"], SKILL_DIR)
            )

        for raw_path in paths:
            norm = normalize_path(raw_path, cwd)
            if not norm:
                return "deny", (
                    "[safedelete] 已拦截：%s，路径解析失败：" % rule["desc"]
                    + "`%s`。请改写为明确路径。" % raw_path
                )
            if is_protected(norm, config.get("protected_exact_paths", [])):
                return "deny", (
                    "[safedelete] 已拦截：%s 的目标 `%s` 属于受保护根路径，"
                    "白名单不生效。该操作会造成不可逆损失。" % (rule["desc"], raw_path)
                )
            # cwd 之外的绝对路径用严格判定（仅前缀/后缀白名单），避免跨项目误放
            cwd_norm = (cwd or "").replace("\\", "/").rstrip("/").lower()
            outside = is_absolute(norm) and cwd_norm and not norm.lower().startswith(cwd_norm + "/")
            if outside:
                if not is_whitelisted_strict(norm, config):
                    return "deny", (
                        "[safedelete] 已拦截：%s 的目标 `%s` 位于当前工作目录之外且不在安全白名单内。\n"
                        "跨目录删除风险更高，一律拦截。如需执行，请改用可回滚的安全删除：\n"
                        "  python %s/scripts/safe_delete.py %s\n"
                        "或先确认再手动放行。" % (rule["desc"], raw_path, SKILL_DIR, raw_path)
                    )
            elif not is_whitelisted(norm, config, cwd):
                return "deny", (
                    "[safedelete] 已拦截：%s 的目标 `%s` 不在安全白名单内。\n"
                    "规则：全路径一律拦截，仅放行构建产物/缓存/临时目录（配置见 %s）。\n"
                    "如果确认要删除，请改用可回滚的安全删除：\n"
                    "  python %s/scripts/safe_delete.py %s\n"
                    "或被你手动确认后，把该路径加入配置白名单再执行。"
                    % (rule["desc"], raw_path, CONFIG_PATH, SKILL_DIR, raw_path)
                )
    return "allow", ""


def main():
    try:
        raw = sys.stdin.buffer.read()
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except Exception as exc:
        # 连 payload 都解析不了时，放行以免阻塞正常工作
        sys.stderr.write("[safe-delete-guard] payload parse failed: %r\n" % exc)
        allow()

    try:
        config = load_config()
    except Exception as exc:
        sys.stderr.write("[safe-delete-guard] config load failed: %r\n" % exc)
        allow()

    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}
    command = tool_input.get(config.get("tool_command_field", {}).get(tool_name, "command"), "")
    cwd = payload.get("cwd") or os.getcwd()

    try:
        decision, reason = evaluate(config, tool_name, command, cwd)
    except Exception as exc:
        log_event(config, payload, "error", repr(exc))
        sys.stderr.write("[safe-delete-guard] evaluate failed: %r\n" % exc)
        decision = "deny" if config.get("on_error") == "deny" else "allow"
        reason = (
            "[safe-delete-guard] 规则脚本执行异常，已按 on_error=deny 拦截。"
            if decision == "deny"
            else ""
        )

    if decision == "deny":
        log_event(config, payload, "deny", reason)
        deny(reason)
    log_event(config, payload, "allow", "")
    allow()


if __name__ == "__main__":
    main()
