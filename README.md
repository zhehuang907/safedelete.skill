# safedelete — AI 编码助手的防误删守卫

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

一个可被任意支持 `PreToolUse` hook 的 AI 编码工具（WorkBuddy / CodeBuddy / Claude Code 等）复用的「防误删」Skill。

它解决一个真实痛点：**AI 助手在处理任务时，可能顺手敲出 `rm -rf`、`git reset --hard`、`git clean -fd` 之类的命令，把你的重要文件一扫而空。**

safedelete 用两层防线堵住这个口子：

1. **硬拦截层**（`scripts/guard.py`）——注册成 `PreToolUse` hook，在命令真正执行前判定并 `deny`。与模型的「记不记得规矩」无关，命中即拦。
2. **软流程层**（`SKILL.md` + `scripts/safe_delete.py`）——教模型在「必须删除」时改用可回滚的安全删除：把文件移入回收站，而非直接销毁。

---

## 特性

- **真正的硬拦截**：不只是「好心提醒」，hook 在工具执行前 `deny`，命令根本不发生。
- **覆盖广度**：`rm` / `rmdir` / `del` / `Remove-Item` / `find -delete` / `xargs rm` / `rsync --delete` / `truncate` / `git clean` / `git reset --hard` / `git checkout --` / `git rm` / `shutil.rmtree` / `fs.rmSync` / `dd of=/dev/*` / 重定向 `> 已存在文件` 清空。
- **不误伤正常开发**：构建产物与缓存目录（`node_modules`、`dist`、`build`、`.cache`、`__pycache__` 等）、日志后缀、系统临时目录默认放行；`git clean -n` dry-run、`git restore --staged`、`git rm --cached`、`git checkout -b` 等安全操作不拦。
- **保守兜底**：命令无法判定明确路径（用了变量/通配）、或目标在受保护根路径（如 `~`、`/`）、或跨出当前工作目录时，一律拦截。
- **零依赖、跨平台**：纯 Python 标准库，Windows / macOS / Linux 通用。
- **可配置**：所有规则与白名单在 `scripts/guard_config.json`，即时生效。
- **自带自检**：`scripts/test_guard.py` 端到端验证拦截/放行/回滚链路。

---

## 安装

```bash
# 放到你的 Skill 目录（WorkBuddy 示例）
# 其余工具把本目录放到对应 skills 位置即可

# 1) 安装 hook（默认 WorkBuddy）
python safedelete/scripts/install_hooks.py --dry-run   # 先预演
python safedelete/scripts/install_hooks.py             # 真安装

# 2) 同时覆盖 CodeBuddy / Claude Code
python safedelete/scripts/install_hooks.py --target all

# 3) 确认
python safedelete/scripts/install_hooks.py --status
```

> 安装后请**开启新会话**（hook 在会话启动时加载）。
> 验证：新会话里执行 `rm -rf <任意普通目录>`，应被拒绝并给出原因。

### 卸载

> ⚠️ 移动或删除本 Skill 目录前，**必须**先卸载，否则 `settings.json` 会留下指向失效脚本的死 hook，导致每次 Bash 调用报错。

```bash
python safedelete/scripts/install_hooks.py --uninstall --target all
```

---

## 安全删除（被拦截后该怎么做）

```bash
python safedelete/scripts/safe_delete.py <路径> [更多路径]   # 移入回收站
python safedelete/scripts/safe_delete.py --list              # 查看可回滚条目
python safedelete/scripts/safe_delete.py --restore 0001      # 按 ID 还原
python safedelete/scripts/safe_delete.py --restore all       # 全部还原
python safedelete/scripts/safe_delete.py --purge --older-than 30d
```

回收站默认在 `~/.workbuddy/trash`，条目记录在 `manifest.jsonl`。

---

## 配置

编辑 `scripts/guard_config.json`：

- `whitelist_dir_components` / `whitelist_suffixes` / `whitelist_path_prefixes`：放行规则。
- `protected_exact_paths`：必拦的根路径（白名单不生效）。
- `danger_commands` / `danger_patterns`：拦截规则，支持 `always_deny`（不豁免白名单）。
- `on_error`：`allow`（脚本故障放行，默认）或 `deny`（安全优先，但脚本出错会锁死终端）。

---

## 规则原理（简述）

判定顺序：廉价预检（无危险关键词直接放行）→ 正则/命令规则命中 → 路径可判定性 → 受保护根 → 跨目录严格化 → 白名单 → 其余拦截。
详见 `SKILL.md` 与 `scripts/guard.py` 中的注释。

---

## 开发 / 自检

```bash
python safedelete/scripts/test_guard.py
```

新增拦截场景时，请同步往 `test_guard.py` 的 `DENY_CASES` / `ALLOW_CASES` 加用例。

---

## 已知边界

1. **只拦命令文本**：模型若先写一个删除脚本再执行，命令层看到的是 `python script.py`，靠 SKILL.md 的软约束兜底。
2. **不拦截 `Write` 覆盖写入**：覆盖前请自行确认原内容已备份。
3. **跨目录操作一律从严**：cwd 之外的绝对路径不会因中间层级含白名单目录名而放行。

---

## License

[MIT](LICENSE)
