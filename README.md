# MA2

Windows 上的 headless 多 Agent 编排。

## 设计前提

**Agent 是 headless 子进程，不是交互式终端会话。**

`claude -p` / `codex exec` 读 stdin、写 stdout、跑完退出，就是普通子进程。因此
控制面不需要 PTY、不需要终端多路复用器、不需要按键注入。编排器直接 spawn 进程、
读事件流、写文件协议。

这个前提把 handoff.md 里"并行、隔离、派发、厂商中立、程序化接口"这几项从
"需要专门工具"降级成了"几行代码"。真正需要额外投入的只剩**人工实时观察**，
而那一层是可选的、且与控制面完全解耦。

## 当前进度：第二步（并行 + worktree 隔离）

已完成：

- **第一步** 单 Agent 启动 → 事件流原样落盘 → 增量归约出状态 → 产出 `result.json`，含超时执行与失败路径。
- **第二步** 三阶段编排器：串行建 worktree → 并行派发 Agent → 串行提交并收尾。N=3 实测通过。

刻意未做：重试策略（第三步）、多 Agent 汇总（第四步）。

## 用法

```powershell
D:/python/anaconda/envs/th123/python.exe -m ma2 run   plans/parallel3.json
D:/python/anaconda/envs/th123/python.exe -m ma2 show  runs/<run_id>
D:/python/anaconda/envs/th123/python.exe -m ma2 prune runs/<run_id>
```

`run` 常用开关：`--cleanup`（跑完回收工作区）、`--no-commit`（不自动提交）、
`--max-parallel N`（plan 里的 `max_parallel` 优先）。

无第三方依赖，只用标准库。

## 目录布局

```
runs/<run_id>/
    run.json                        运行元数据与最终汇总
    status.json                     全部 Agent 的聚合实时状态 —— 观察面只读这一个文件
    agents/<id>.jsonl               原始事件流，只追加 —— 审计与回放的唯一依据
    agents/<id>.status.json         单 Agent 增量状态
    agents/<id>.result.json         正式产物，交给汇总层
    agents/<id>.stderr.log          仅在 stderr 非空时生成
    workspaces/<id>/                Agent 工作目录（worktree 模式下由 git 创建）
```

所有 JSON 写入走「同目录临时文件 + `os.replace`」，读者不会读到写了一半的文件。

## 执行模型

```
阶段 1  prepare   串行   git worktree add，任一失败则整体回滚
阶段 2  dispatch  并行   ThreadPoolExecutor，每 Agent 一线程
阶段 3  collect   串行   提交改动 → 收集 diff → 写汇总 → 按需回收
```

阶段 1 必须串行：`git worktree add` 会写目标仓库的 refs 和 index，并发必撞
`index.lock`。阶段 2 可以并行：Agent 全程阻塞在子进程管道上，是 I/O 密集型，
线程模型足够，不需要多进程。

## 实测结论

以下均为 2026-08-28 在 claude 2.1.250 / Windows 11 上实际验证，不是推断。

### 1. stream-json 的事件 schema

`claude -p --output-format stream-json --verbose` 产出四类事件：

| 事件 | 关键字段 |
|---|---|
| `system` / `init` | `session_id` `cwd` `model` `permissionMode` `tools[]` |
| `assistant` | `message.content[]`（`tool_use` / `text`）`timestamp` `uuid` |
| `user` | `message.content[]`（`tool_result`）`tool_use_result` |
| `result` | `is_error` `subtype` `stop_reason` `terminal_reason` `num_turns` `duration_ms` `total_cost_usd` `usage` `permission_denials[]` `result` |

`result` 事件是一份完整的运行摘要。**一份 JSONL 同时承担审计流、状态判定、
实时观察三个职责**，因此系统全程不需要抓屏或匹配终端缓冲区（handoff §15）。

### 2. 凭据边界几乎是免费的

子进程环境按白名单构造，只有 15 个变量，**不转发任何 `ANTHROPIC_*`**，
Agent 仍能正常认证 —— `claude` 自己从 `~/.claude/settings.json` 读取认证配置。

意味着编排器全程不接触凭据：凭据不出现在命令行、不出现在子进程环境、
不出现在 `plan.json`、不出现在事件流里。handoff §9 基本自动满足。

反过来这也是不把多路复用器放进控制面的实质理由：那种架构下 Agent 继承的是
复用器进程的环境，最小化环境反而更难做。

### 3. headless 不会因权限询问卡死

工具不在 `--allowedTools` 白名单时，Agent **不会挂起等待输入**，而是被拦截、
记入 `permission_denials`、正常走完并给出解释性回答。

但有个坑：这种情况下 `status` 仍然是 `completed`，`stop_reason` 仍然是
`end_turn`。**`status == completed` 不足以判定任务成功**，编排器必须同时检查
`permission_denials` 是否为空，否则会把一次"什么都没干成"的运行当成功收进汇总。
这条留给第三步处理。

### 4. Windows 进程树：超时必须用 taskkill /T

npm 装的 `claude` 是 `claude.cmd`，一个 cmd.exe 包装器，真正干活的是它派生的
node 子进程。`proc.kill()` 只杀 cmd.exe，node 会继续运行并持有 stdout 管道，
读取循环因此一直阻塞。

实测后果：`timeout_sec=8` 的任务实际跑了 **6 分 40 秒**，把整篇长文写完才结束，
超时只被"检测"到、没被"执行"。改用 `taskkill /F /T /PID` 杀整棵进程树后，
同一个 plan 的墙钟从 6m40s 降到 18.6s。

这是纯 Windows 问题，POSIX 上不会遇到，而 Windows 恰好是本项目的硬约束。

### 5. 全局 CLAUDE.md 会污染 worker 的结构化输出

worker 继承用户的全局 `CLAUDE.md`，其中的输出格式约定（比如强制追加"行动轨迹"）
会混进 `result` 事件的正文，破坏下游解析。

对策：每个任务用 `system_suffix`（`--append-system-prompt`）显式声明输出契约，
说明"回复会被程序解析，只输出结果本身"。已在 `plans/hello.json` 中示范。

### 6. worktree 隔离确实成立

`plans/parallel3.json` 让三个 Agent **写同一个文件名 `NOTES.md`**，各自要求不同的
首行。如果隔离失效，它们必然互相覆盖。

实测：三个工作区各拿到自己那份内容，仓库根目录没有 `NOTES.md`，`main` 未被触碰。
三个 Agent 同毫秒启动、8.5s / 9.3s / 11.3s 分别结束，墙钟 17.3s —— 并行成立，
worktree 隔离成立。

### 7. 不自动提交，回收就等于销毁

Agent 几乎不会自己 `git commit`，成果只存在于工作目录。而 `git worktree remove`
必须加 `--force` 才能删掉脏工作区 —— 一删就把未提交的改动一并删了。

第一次跑完 `remove` 后实测：分支还在，但 `git show <branch>:NOTES.md` 报
`path does not exist`，分支停在 `main` 的 commit 上，**Agent 的产出全没了**。
"保留分支"在没提交的前提下是句空话。

因此 `collect` 阶段默认先 `git add -A && git commit` 到该 Agent 的分支（提交者
身份显式指定为 `ma2-orchestrator`，不依赖机器上恰好配了 `user.name`），之后
工作区才是一次性的。`prune` 同样默认拒绝删除脏工作区，除非显式 `--force`。

这条对第四步有直接影响：汇总层应该从**分支**读产出，而不是从可能已被回收的
工作目录读。

### 8. worktree 注册会无限累积

每次 run 留下 N 个 worktree 注册在 `.git/worktrees` 里，而 `runs/` 是 gitignore 的。
用户手工删掉 `runs/` 目录后注册信息仍在，git 会一直把它们报成 prunable。
所以回收需要正规出口：`ma2 prune runs/<run_id>`。

## 后续

- **第三步** 超时重试策略；把 `permission_denials` 非空纳入失败判定
- **第四步** 汇总 agent，N 份 `result.json` → `final.md`（从分支读，不从工作目录读）
- **待验证** `codex exec` 的结构化事件是否与 `stream-json` 等价。厂商中立是
  handoff §1 的硬要求，这个缺口目前仍未验证。
- **之后** 再决定是否需要观察面。跑通前四步才有依据回答这个问题。
