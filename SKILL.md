---
name: safedelete
description: 防止 AI 编码助手误删文件。当任务涉及删除、清理、重置、重构、初始化目录、清空缓存，或出现 rm / del / Remove-Item / find -delete / xargs rm / git clean / git reset --hard / git rm / shutil.rmtree / fs.rm / dd of=/dev / 重定向截断等破坏性操作时使用。提供 PreToolUse 硬拦截 + 可回滚的安全删除脚本，删除前必须走回收站，禁止直接用 rm -rf。
description_zh: "防误删守卫：硬拦截破坏性删除命令，并提供可回滚的安全删除"
description_en: "Blocks destructive delete commands via PreToolUse hook and provides a rollback-capable safe delete"
version: 1.1.0
agent_created: true
allowed-tools: Read,Write,Edit,Bash,PowerShell
---

# 防误删守卫（safedelete）

本 Skill 用于阻止 AI 编码助手（WorkBuddy / CodeBuddy / Claude Code 等）在处理任务时误删文件。
两层职责分离：

1. **硬拦截层**（`scripts/guard.py`，注册为 `PreToolUse` hook）——在 Bash / PowerShell 执行前阻断破坏性删除命令。与模型意愿无关，命中即 `deny`。
2. **软流程层**（本文件 + `scripts/safe_delete.py`）——教你在必须删除时怎么做才不出事：移入回收站，可一键回滚。

> 生效前提：已执行 `scripts/install_hooks.py`，且开启了新会话。用 `--status` 可确认。

---

## 一、被拦截时怎么办（最重要）

当工具返回 `permissionDecision: deny` 且原因以 `[safedelete]` 开头时：

**必须遵守三条：**

1. **不要换一种写法绕过拦截。** 以下都属绕过，禁止：
   - 改用 `python -c "shutil.rmtree(...)"`、`node -e "fs.rmSync(...)"` 等间接删除
   - 把删除逻辑写进脚本文件再执行，或 `find ... | xargs rm`
   - 拆成多条命令分批删除
   - 用 `mv` 到 `/dev/null`、覆盖重定向 `> file` 等方式变相销毁
2. **先向用户确认**，说明要删什么、为什么删、影响范围。
3. **确认后走安全删除**，用下方的脚本，而不是原始 `rm`。

---

## 二、安全删除（可回滚）

```bash
# 删除（实际是移入回收站，可还原）
python ~/.workbuddy/skills/safedelete/scripts/safe_delete.py <路径> [更多路径]

python ~/.workbuddy/skills/safedelete/scripts/safe_delete.py --list          # 查看可回滚条目
python ~/.workbuddy/skills/safedelete/scripts/safe_delete.py --restore 0001  # 按 ID 还原
python ~/.workbuddy/skills/safedelete/scripts/safe_delete.py --restore all   # 全部还原
python ~/.workbuddy/skills/safedelete/scripts/safe_delete.py --purge --older-than 30d
```

回收站默认在 `~/.workbuddy/trash`，条目记录在 `manifest.jsonl`。加 `--json` 可得到结构化输出。

**批量删除前先自检范围**：`--list` 确认清单，再执行，不要一次扫掉整个目录树。

---

## 三、拦截规则

判定顺序（见 `scripts/guard.py`）：

| 步骤 | 条件 | 结果 |
|---|---|---|
| 0 | 命令不含任何危险关键词 | 直接放行（廉价预检，零开销） |
| 1 | 命中「无法静态判定范围」类（`find -delete`、`rsync --delete`、`truncate`、`git clean/reset/checkout/rm`、`xargs rm`、`dd of=/dev/*`、`shutil.rmtree` 等语言 API、重定向 `> 已存在文件`） | **拦截**，白名单不豁免 |
| 2 | 命令中无明确路径（变量、通配） | **拦截**（保守） |
| 3 | 目标落在受保护根路径（`~`、`/`、`C:\` 等） | **拦截** |
| 4 | 目标位于当前工作目录**之外**的绝对路径 | 仅放行「前缀白名单（系统临时目录）/ 后缀白名单（.log 等）」，其余 **拦截**（避免跨项目误放） |
| 5 | 目标位于 cwd 之内，且所有目标均在白名单（构建产物 / 缓存 / 临时目录） | 放行 |
| 6 | 其余 | **拦截** |

**默认放行**的典型路径：`node_modules`、`dist`、`build`、`out`、`.next`、`target`、`bin`、`obj`、`venv`、各类缓存目录、`tmp`，以及 `.log/.tmp/.bak/.pyc` 等后缀和当前机器的临时目录。

修改规则：编辑 `scripts/guard_config.json`，**即时生效，无需重启**。

---

## 四、安装与卸载

```bash
python ~/.workbuddy/skills/safedelete/scripts/install_hooks.py --status
python ~/.workbuddy/skills/safedelete/scripts/install_hooks.py --dry-run
python ~/.workbuddy/skills/safedelete/scripts/install_hooks.py                  # WorkBuddy
python ~/.workbuddy/skills/safedelete/scripts/install_hooks.py --target all     # 含 CodeBuddy / Claude Code
python ~/.workbuddy/skills/safedelete/scripts/install_hooks.py --uninstall --target all
```

> ⚠️ **迁移 / 删除本 Skill 前，务必先 `--uninstall`**。hook 命令里写死了 `guard.py` 的绝对路径；
> 若直接移动或删除目录而没先卸载，settings.json 会留下指向失效路径的死 hook，
> 导致每次 Bash 调用都报错。卸载脚本已兼容历史目录名 `safe-delete-guard`。

修改配置前会自动备份为 `settings.json.wbguard.bak`。安装后需**开启新会话**才生效。

---

## 五、已知边界（务必知悉）

这些是设计取舍，不是 bug：

1. **只拦命令文本。** 若先写入一个删除脚本再执行它，命令层看到的是 `python script.py`。此时靠本文件的第一条软约束兜底。
2. **脚本异常默认放行**（`on_error: allow`）。目的是不因脚本故障锁死终端；想要安全优先可改为 `deny`，代价是脚本出错时所有 Bash 都会被拒。
3. **跨目录操作一律从严**：路径在 cwd 之外时，不会因路径中某级目录名为 `build`/`node_modules` 就放行，必须命中前缀或后缀白名单。
4. **不拦截覆盖写入。** `Write` 工具覆盖已有文件不在拦截范围，覆盖前请自行确认原内容已备份。

---

## 六、维护

改完规则或脚本后跑自检，确保没有把正常开发流程误杀：

```bash
python ~/.workbuddy/skills/safedelete/scripts/test_guard.py
```

新增拦截场景时，同步往 `test_guard.py` 的 `DENY_CASES` / `ALLOW_CASES` 加用例，避免以后改动把规则改坏。
