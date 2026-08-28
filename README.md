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

## 当前进度：第四步（汇总）

已完成：

- **第一步** 单 Agent 启动 → 事件流原样落盘 → 增量归约出状态 → 产出 `result.json`，含超时执行与失败路径。
- **第二步** 三阶段编排器：串行建 worktree → 并行派发 Agent → 串行提交并收尾。N=3 实测通过。
- **第三步** 判定层与重试：`status`（协议事实）与 `verdict`（成败判定）分离，权限被拒不重试，重试前重置工作区、每次尝试各自留审计流。
- **第四步** 汇总层：N 份 `result.json` → 一份 `final.md`。机械汇总无条件产出，综合汇总（一个 Agent）显式开启且允许失败；产出一律从**分支**读。
- **测试** 186 个测试覆盖协议、归约、执行器、工作区、判定、汇总与编排，全部离线运行。

四步都已跑通，`plans/aggregate.json` 是端到端实测用例。

## 用法

```powershell
D:/python/anaconda/envs/th123/python.exe -m ma2 run   plans/aggregate.json --aggregate
D:/python/anaconda/envs/th123/python.exe -m ma2 final runs/<run_id>
D:/python/anaconda/envs/th123/python.exe -m ma2 show  runs/<run_id>
D:/python/anaconda/envs/th123/python.exe -m ma2 prune runs/<run_id>
```

`final` 打的是交付物，`show` 打的是各 Agent 的原始回答（排障用）。

`run` 常用开关：`--cleanup`（跑完回收工作区）、`--no-commit`（不自动提交）、
`--max-parallel N`（plan 里的 `max_parallel` 优先）、`--retries N`（额外重试次数，
压过 plan 的 run 级默认，task 级仍最高优先）、`--aggregate` / `--no-aggregate`
（跑不跑综合层；机械汇总 `final.md` 无论如何都会写）。

跑完打出的表按 `verdict` 而不是 `status` 算成败，失败的行末尾附原因：

```
agent          verdict  status     try branch                 dirty     sec  cost
----------------------------------------------------------------------------------------
denial-01      FAIL     completed    1 -                          -     8.7 $0.1350  permission_denied
timeout-01     FAIL     timeout      2 -                          -    17.0 ≥$0.0000  timeout

0/2 ok（status=completed 的有 1 个）  ≥$0.1350
```

`status=completed` 配 `verdict=FAIL` 不是矛盾，是两层事实：进程确实正常跑完了，
但它什么都没干成。`≥` 表示费用是下界而非实测值（见结论 12）。

无第三方依赖，只用标准库。

## 测试

```powershell
D:/python/anaconda/envs/th123/python.exe -m unittest discover -s tests -t .
```

186 个测试，约 70 秒，**不花钱、不联网**。

关键在 `tests/fake_agent.py`：一个 claude 兼容的测试替身，按 `--scenario` 吐脚本化的
stream-json。真 claude 跑一次 N=3 隔离测试要 $0.9 / 17s，替身把同一个测试变成免费的，
于是可以常跑、可以在改动前后对比。`task.launcher` 是它的接入点 —— 这个字段同时也是
厂商中立（§1）的接缝：换一个 claude 兼容的可执行文件，编排层不用改。

替身能演出每一种实测撞到过的失败形态，其中三个直接对应已修的 bug：

| scenario | 演的是什么 |
|---|---|
| `hang-tree` | 派生持有 stdout 的孙进程 —— 结论 4 的回归夹具 |
| `garbage` | 非 JSON 行必须原样进审计流并被计数 |
| `silent` | 进程正常退出却没有 result 事件 |
| `denial` | `completed` 但 `permission_denials` 非空 —— 结论 3 |
| `empty` | `completed` 但一个字都没回答 |
| `write` | 真写文件，隔离测试靠它证明互不覆盖 |
| `write-crash` | 留下半成品再失败 —— 重置工作区的夹具 |
| `flaky` | 前 N 次失败、之后成功，失败形态可选（崩溃/挂起/权限） |
| `echo` | 把收到的 prompt 原样当回答吐回来 —— 汇总测试的夹具 |

超时那两条测试用**墙钟**断言，而不是断言状态字段。因为结论 4 的教训正是：超时被
"检测"到很容易，被"执行"才难，只有墙钟能区分这两者。

重试测试同样不满足于"重试发生了"：`flaky` 每次尝试写 `<n>-NOTES.md`，工作区有没有
被重置可以被直接看见；每条"重试救回来了"的用例都配一条不给重试的反向对照，
证明成功来自重试而不是夹具本来就会成功。

汇总测试也是同一个路子。断言"汇总跑过了"证明不了简报有没有真的送到汇总 Agent
手里 —— `echo` 夹具把收到的 prompt 原样吐回来，于是"各 Agent 的 prompt、回答、
分支上的文件内容都进了简报"变成三句可断言的话。另一条
`test_aggregation_reads_branches_not_workspaces` 先断言工作目录确实已经不存在，
再断言产出仍然读得到，否则它可能是靠残留目录通过的。

## 目录布局

```
runs/<run_id>/
    run.json                        运行元数据与最终汇总
    final.md                        交付物：机械汇总 +（可选）综合汇总
    brief.md                        汇总 Agent 实际看到的输入
    status.json                     全部 Agent 的聚合实时状态 —— 观察面只读这一个文件
    agents/<id>.attempt<N>.jsonl    原始事件流，按尝试分文件，只追加 —— 审计与回放的唯一依据
    agents/<id>.status.json         单 Agent 增量状态
    agents/<id>.result.json         正式产物，交给汇总层
    agents/<id>.stderr.log          仅在 stderr 非空时生成
    workspaces/<id>/                Agent 工作目录（worktree 模式下由 git 创建）
```

事件流按尝试分文件，是因为重试**恰恰最需要保留失败那一次的证据**。让第二次尝试
覆盖同一个文件，等于把最该留证的那份审计流删掉，与 §13「只追加」直接冲突。

所有 JSON 写入走「同目录临时文件 + `os.replace`」，读者不会读到写了一半的文件。

## 执行模型

```
阶段 1  prepare    串行   git worktree add，任一失败则整体回滚
阶段 2  dispatch   并行   ThreadPoolExecutor，每 Agent 一线程
阶段 3  collect    串行   提交改动 → 收集 diff → 写汇总 → 按需回收
阶段 4  aggregate  串行   从分支读产出 → 写 brief.md →（可选）综合 → 写 final.md
```

阶段 1 必须串行：`git worktree add` 会写目标仓库的 refs 和 index，并发必撞
`index.lock`。阶段 2 可以并行：Agent 全程阻塞在子进程管道上，是 I/O 密集型，
线程模型足够，不需要多进程。阶段 4 必须排在阶段 3 之后：产出要先被提交到分支上，
汇总才读得到（结论 7）。

## 判定与重试

`ma2/policy.py` 是纯判断层：不做 IO，只把一份 `result` 文档映射成一个 `Verdict`。

**`status` 和 `verdict` 是两回事，不能合并。**

- `status` 是**协议层事实**：事件流归约出来的终态（`completed` / `failed` /
  `timeout`）。它回答"这个进程跑成什么样了"。
- `verdict` 是**判定层结论**：这次运行算不算数。它回答"我们要不要认这个结果"。

一次被权限全拦下的运行，`status` 是 `completed`（进程确实正常走完了），
`verdict` 是 `FAIL(permission_denied)`（它什么都没干成）。把 `status` 直接改写成
`failed` 会同时销毁前一个事实 —— 之后再想区分"进程崩了"和"进程好好跑完但被拦了"
就没有依据了。审计流要留住发生了什么，判定层负责说这不算数。

失败原因与是否重试：

| 原因 | 触发条件 | 重试？ |
|---|---|---|
| `timeout` | watchdog 触发、进程被杀 | ✅ |
| `crashed` | 进程退出但没有 result 事件，或 `is_error` | ✅ |
| `empty_answer` | `completed` 但回答是空的 | ✅ |
| `no_changes` | 开了 `require_changes` 却什么都没改 | ✅ |
| `permission_denied` | `permission_denials` 非空 | ❌ |

**权限被拒不重试**：同样的 `allowed_tools` 重试多少次都会被同样拦下，重试纯粹是
把钱烧两遍。这是配置错误，不是抖动。只要有一个原因不可重试，整条判定就不重试。

plan 里可配（task 级 > run 级 > 内置默认）：

| key | 默认 | 含义 |
|---|---|---|
| `retries` | `0` | **额外**尝试次数，所以最多跑 `retries + 1` 次 |
| `retry_delay_sec` | `2.0` | 线性退避基数 |
| `reset_between_attempts` | `true` | 重试前把工作区恢复干净 |
| `checks.deny_permission_denials` | `true` | 权限被拒算失败 |
| `checks.require_answer` | `true` | 空回答算失败 |
| `checks.require_changes` | `false` | 要求真的改了文件才算成功 |

重试前默认重置工作区，因为 `claude -p` 每次调用都是无状态的：第二次尝试拿到的是
同一段 prompt、没有上一次的记忆。让它看见上次留下的半成品，行为只会更难预测。
重置只丢未提交的改动（`git reset --hard HEAD` + `git clean -fd`），已经落到分支上的
历史不动。上一次尝试的完整记录留在它自己的 `attempt<N>.jsonl` 里，不会失传。

`plans/retry.json` 是这两条命题的实测用例：一个必被权限拦下的任务（给 3 次重试，
实际只跑 1 次）配一个必然超时的任务（给 1 次重试，跑满 2 次）。

## 汇总

`ma2/aggregate.py` 把汇总切成**两层**：

| 层 | 是什么 | 花钱？ | 何时产出 |
|---|---|---|---|
| 机械层 | 纯代码。谁成功了、失败原因、改了哪些文件、产出在哪个分支、花了多少钱 | 否 | **无条件** |
| 综合层 | 一个 Agent。把 N 份回答读成一段有观点的正文 | 是 | 显式开启 |

**为什么不整层交给一个 Agent。** 总结器失败是常态之一：超时、限流、跑题、权限被拦。
如果 `final.md` 只有 Agent 能写，总结器一挂，整次 run 的成果就只剩一堆散落的 JSON ——
那正是结论 7「回收动作静默销毁产出」换了个位置重演。所以综合失败时 `final.md` 照样
写出来，失败原因写在正文的位置上，明细、各 Agent 回答、产出分支一个都不少。

**为什么综合层要显式开启。** 它要花钱，而 N 小的时候人自己读三份回答比读一份总结
更快。只有 N 大到人不愿意逐份读，这笔钱才划算。开启方式：plan 里加
`"aggregator": {...}`，或命令行 `--aggregate`；`--no-aggregate` 可以把 plan 里配了的
一键关掉（排障重跑时不必为每次都付钱）。

**为什么只从分支读。** `--cleanup` 之后 `workspaces/<id>/` 就不存在了，分支才是产出的
持久所在（结论 7）。`workspace.read_branch()` 是汇总层唯一的取数入口，diff 的 base 用
建工作区时记下的 commit sha 而不是分支名 —— 分支名会随主线推进而漂移。

**汇总 Agent 就是一个普通 task。** 同一个 `run_agent`、同一套超时、同一套判定与重试。
它没有理由享受特殊待遇：它一样会超时、一样会跑题。简报整份走 stdin，因此它
`allowed_tools` 为空也能读到全部输入 —— 少一个工具就少一类失败面。

它不算进 `ok/total`（它不是参与运算的 Agent，算进去会把"3/3 成功"写成"3/4"），
但**费用并进总额**：少算一个 Agent 的钱就是账面比现实好看，和结论 12 同一个口径。

`plans/aggregate.json` 是端到端实测用例：三个 Agent 各为一种终止 Windows 进程树的
做法辩护，结论必然互相冲突，汇总 Agent 的任务不是复述而是裁决。

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

第三步的处理方式是加一层 `verdict`，而不是把 `status` 改写成 `failed` ——
两个事实都要留住（见「判定与重试」）。实测 `plans/retry.json` 里的 `denial-01`：
`status=completed` 与 `verdict=FAIL(permission_denied)` 同时成立，汇总打出
`0/2 ok（status=completed 的有 1 个）`，退出码 1。按旧口径这一跑会被报成
"1/2 完成"。

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

**但这个对策只是削弱，不是根治。** `plans/aggregate.json` 实测：三个 worker 都带了
上面那段 `system_suffix`，仍有两个在回答末尾附上了"**行动轨迹：**"。

后续用同一个中性 prompt（不含任何抑制指令）做了三组对照，只改一个变量：

| 条件 | worker 输出行动轨迹 |
| --- | --- |
| 在全局 `CLAUDE.md` 里加一条"headless 时本节不适用"的豁免 | **是**（无效） |
| `system_suffix` 声明 headless 输出契约 | 部分（3 个 worker 中 2 个仍输出） |
| prompt 正文里写"不要输出别的任何内容" | 否 |
| 把该节从全局 `CLAUDE.md` 整节删除 | 否 |

豁免条款失效的原因不是"`CLAUDE.md` 打不过自己"，而是**这条规则要求 worker 判断
"我是不是 headless"，而它并不知道** —— `claude -p` 的上下文里没有任何东西告诉它
自己是被编排器调用的。`system_suffix` 之所以部分有效，恰恰因为它直接*告知*了这个
事实；prompt 正文最有效，因为它无条件、不需要判断。

一般化的教训：**想让 worker 少做某事，规则必须无条件，或者显式补上它缺失的那个
前提事实。**"某某情况下不要 X"这种依赖自我认知的条件句，在 headless 下不可靠。

即便如此，下游仍不能假定回答是干净的 —— worker 还会复述任务、加前言、包代码围栏。
汇总层因此按"回答里可能混着无关格式"设计：把原文原样转交给综合层，让模型自己忽略
噪声，而不是靠正则去剥。`aggregate` 那次实测里综合层确实没被那两段尾巴带偏。

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

这条直接决定了汇总层的取数方式：从**分支**读产出，而不是从可能已被回收的
工作目录读（见「汇总」）。

### 8. worktree 注册会无限累积

每次 run 留下 N 个 worktree 注册在 `.git/worktrees` 里，而 `runs/` 是 gitignore 的。
用户手工删掉 `runs/` 目录后注册信息仍在，git 会一直把它们报成 prunable。
所以回收需要正规出口：`ma2 prune runs/<run_id>`。

### 9. 补测试当场抓到两个真 bug

测试不是给已知正确的代码盖章，它当场抓到了两处此前没人发现的问题：

**文件描述符泄漏。** `run_agent` 从不关闭子进程的 stdin/stdout/stderr 管道。
单跑一次看不出来，但编排器会并行反复起 Agent，泄漏会累积。测试跑完刷出一片
`ResourceWarning: unclosed file` 才暴露。现已改为 try/finally 中统一关闭，并且
无论正常结束还是中途抛异常，都不会留下活着的子进程。测试用
`-W error::ResourceWarning` 把它钉死。

**非 ASCII 路径被 git 转义成八进制。** `git status --porcelain` 默认把中文文件名
输出成 `"\346\226\260.md"`，这些字符串会原样进 `result.json`，汇总层拿到的是乱码。
所有 git 调用现已带上 `-c core.quotePath=false`。

两个都是"跑得通但不对"的问题，只有写测试才会撞上。

### 10. 测试替身必须在编码上也忠实

编排器按 UTF-8 读子进程 stdout。真 claude 是 node，本来就吐 UTF-8；而 Windows 上
Python 的 stdout 默认走系统 ANSI 代码页（本机 GBK），假 Agent 不显式
`reconfigure(encoding="utf-8")` 就会吐出乱码 —— 测出来的是替身的毛病，不是被测
代码的毛病。

顺带暴露一个潜在的厂商中立风险：如果某个 CLI 按 locale 编码输出，
`errors="replace"` 会让它静默损坏而不是报错。目前 stream-json 假定 UTF-8，
接入第二个厂商时要重新确认这一点。

### 11. 「重置工作区」差点又一次销毁 Agent 的产出

第一版 `reset()` 写的是 `git reset --hard ws.head` —— `ws.head` 是**建工作区时**
记下的那个 commit。这在重试场景下会把分支整个回退，连 Agent 自己已经 commit 过的
成果一起抹掉，正是结论 7 那类"回收动作静默销毁产出"的重演，只是换了个入口。

抓到它的是一条断言"重置保留已有 commit"的测试，写完当场就红了。改成
`git reset --hard HEAD` 后语义才对：重置该丢的是**未提交的改动**，不是已经落到
分支上的历史。

这是测试第三次在合并前抓到真 bug（前两次是 fd 泄漏和 git 八进制转义）。三次都
不是"跑不起来"的错误，而是"跑得通但不对"——只有断言才拦得住。

### 12. 超时的运行在账面上显示成 0 秒 0 美元

`duration_ms` 和 `total_cost_usd` 都只来自 `result` 事件，而**超时的运行根本收不到
那个事件**。于是 `plans/retry.json` 首次实测时，`timeout-01` 在汇总表里显示成
`0.0s / $0.0000` —— 它实际上跑了两次尝试、每次都真的调了 API。一份把失败报得比
现实便宜的账，比没有账更危险。

耗时和费用在这里不对称，处理方式也就不同：

- **耗时编排器自己能测。** `run_agent` 用 `time.monotonic()` 记墙钟写进
  `metrics.wall_ms`，汇总取全部尝试之和（和费用一个口径，只报最后一次同样是瞒账）。
  同一个 plan 修好后重跑，`timeout-01` 从 `0.0s` 变成 `17.0s`（两次 8 秒超时之和）。
- **费用编排器无从得知。** 超时那次花了多少钱只有服务端知道。所以按 0 计入之后
  标记 `cost_is_complete=false`，CLI 打成 `≥$0.0000` —— 明说这是下界，不是实测值。

宁可显示一个承认自己不完整的数字，也不显示一个看起来精确的错数字。

### 13. 综合层便宜，但只有当各方真的分歧时才值这个钱

`plans/aggregate.json` 实测（三个 Agent 各为一种终止进程树的做法辩护）：

| agent | 判定 | 耗时 | 费用 |
|---|---|---|---|
| taskkill | OK | 26.0s | $0.3665 |
| jobobject | OK | 28.0s | $0.3744 |
| ctrlbreak | OK | 43.8s | $0.4059 |
| aggregator | OK | 31.4s | $0.1528 |

干活的三个 $1.1468，加上综合层共 $1.2996。

综合层占总成本 **12%**，输入是 3947 字的简报。它不重跑任何任务、不读任何文件，
只读简报，因此成本随 N 线性增长且系数很小 —— 这一层的经济性不成问题。

真正的问题是它有没有产出机械层给不了的东西。这次实测里它做了三件机械层做不到的事：
指出三方对"遍历进程树会漏"的描述其实完全一致（**只是换了说法，不是分歧**）、
把唯一真分歧收敛到"是否值得付宽限期"和"改动成本"两点、并指出宽限期的具体秒数只有
一家给出、无人交叉检验。这是判断，不是汇编。

反过来说：如果 N 个 Agent 做的是互不相干的并行任务，综合层能说的无非是"它们都完成
了"，那笔钱就白花了。**综合层的价值来自输入之间有交叉，不来自输入的数量。**
所以它是显式开关，不是默认行为。

## 后续

- **待验证** `codex exec` 的结构化事件是否与 `stream-json` 等价。厂商中立是
  handoff §1 的硬要求，这个缺口目前仍未验证。
- **待验证** 若某个 CLI 按 locale 编码输出，`errors="replace"` 会让它静默损坏而不是
  报错（结论 10 顺带暴露的风险）。
- **之后** 四步已跑通，再决定是否需要人工实时观察面。现在有依据回答这个问题了：
  `status.json` 已经把全部 Agent 的实时状态聚合成一个文件，观察面只需要读它。
