# RunPilot 测试与验证

## v0.1.3 — 2026-10-18

- macOS / Python 3.9.6 与 3.12.14 各运行 **135 项 unittest，全部通过**；新增现有 JSON 进度映射、旧文件/归属冲突、阶段切换速率重置、默认串行以及受管退出采样竞态的回归。
- 两版本的无 mock 本地 CPU CLI 闭环通过：普通脚本和已有 JSON 报告示例均得到真实退出 0、`completed` 及完成事件投递；固定程序内 `model_submissions=0`。3.12.14 的闭环额外连续复跑 6 次，仅有完成通知；这不统计外部 Agent/heartbeat 的 token 用量。
- `npx skills --help` 核对存在指定技能 `update [skills...]` 与 `-g/-p` 范围选项；`npx skills add . --list` 识别 `run-pilot`。**未对用户已安装的技能执行更新**。更新模块是按需操作流程，不会自动追踪或安装 GitHub 新版。
- 14 文件安装包经过 YAML/内部引用/源码一致性、ZIP CRC 与 SHA-256 校验，`run-pilot/references/update.md` 已包含在包内。未在 Linux 实机、Windows 原生、真实 GPU/SSH、实际调度器或自动唤醒 Agent 的环境中验证；SSH/远程启动与自动调度安装仍未实现。

## v0.1.2 — 2026-10-17

本轮在 macOS / Python 3.9.6 运行 129 项 unittest，全部通过；无 mock 的本地 CPU CLI 闭环通过，普通 Python 导出任务得到真实退出 0 与完成事件。`example.py` 静态识别不执行项目代码；该脚本的真实受管启动、单次巡检、退出码 0、`completed` 状态及完成事件文件投递均通过。

`npx --yes skills add . --list` 识别到 `run-pilot`；对 GitHub 仓库运行 `--list` 也识别同名技能，但远端仍是 v0.1.1，不能把它当作 v0.1.2 已发布的验证。构建归档包含 13 个文件及 MIT 许可，ZIP CRC、许可文本一致性和 `dist/run-pilot.skill.sha256` 校验通过。

本轮未在 Python 3.12、真实 GPU 或 SSH 远程执行环境复测；已登录目标机上按本地方式运行与从本机发起 SSH 远程启动是两回事。周期服务安装和自动重试仍未实现。

## v0.1.1 — 2026-10-17

在本机 macOS 复测；以下均为合成/CPU 任务，不是生产环境或 GPU 兼容性认证。原始本地日志位于被 `.gitignore` 排除的 `run-pilot-workspace/validation/`，公开仓库提供可复跑源码与摘要。

| 检查 | 实际结果 |
| --- | --- |
| Python 3.9.6 | 129 项 unittest 通过 |
| Python 3.12.14 | 同 129 项通过 |
| 无 mock 的本地 CLI 闭环（两版本） | 现有 JSON 报告任务与普通 Python CSV 导出脚本均完成；真实退出 0、完成事件文件已投递，直接模型提交为 0 |
| 通用任务回归 | 普通入口静态识别、通用进度解析、受约束环境变量覆盖、长运行进程只读观察、`exit_code` 成功/失败/缺回执/只读接管边界 |
| 安装体 | YAML、内部引用、Python 语法检查通过；12 文件 ZIP CRC、逐文件源码一致性与解包后独立 CLI 检查通过；SHA-256 见 `v0.1.1` 标签的 `dist/run-pilot.skill.sha256` |

检出 `v0.1.1` 后可复跑该版本：

```sh
make test
make validate
make build
python3 tools/verify_local_runtime.py
(cd dist && shasum -a 256 -c run-pilot.skill.sha256)
```

本版新增第 4 个普通 Python 任务的**离线决策用例**，但尚未运行新的技能对照评测；下面的 18/18 与 17/18 只属于 v0.1.0 的三组历史用例，不能挪作 v0.1.1 的结果。真实 NVIDIA GPU、跨卡并发、Linux 实机、SSH、远程调度及后台定时器均未通过本次验证；其中 SSH/远程启动、自动 retry/resume 和调度器安装是**尚未实现**，而非仅“未实测”。

## v0.1.0（历史记录）

验证日期：2026-10-17，Asia/Shanghai。真实执行环境为本机 macOS；
下面的 Linux/NVIDIA 相关检查使用合成 fixture，不是训练机兼容性认证。
表内 `run-pilot-workspace/` 路径指向本地原始证据，包含本机路径与执行记录，
不纳入公开仓库；公开仓库保留复跑命令、测试和结果摘要。

### 1. 已运行且通过

| 检查 | 结果 | 证据 |
| --- | --- | --- |
| 系统 Python 3.9.6 | 121 项 unittest 通过 | `run-pilot-workspace/validation/python39-unittest.log` |
| 随应用提供的 Python 3.12.14 | 同 121 项通过 | `run-pilot-workspace/validation/python312-unittest.log` |
| 无 mock 本地 CPU CLI 闭环 | 通过，实际检查脚本直接模型提交为 0 | `run-pilot-workspace/validation/local-cpu-e2e.json` |
| 安装体校验 | 有效 YAML、90 行主指令、13 个本地引用、Python 语法通过 | `python3 tools/build_skill.py --check-only` |
| 归档构建 | 12 个文件，排除评测和缓存，ZIP 校验与 SHA-256 | `dist/run-pilot.skill` 及 `.sha256` |
| 离线决策输出格式 | 6 份 JSON/Markdown 全部通过 | 各 run 的 `format_grading.json` |

复现以下历史数字请先检出 `v0.1.0`；在当前源码上执行这些命令不会得到 v0.1.0 的结果：

```sh
python3 -m unittest discover -s tests -v
python3 tools/verify_local_runtime.py
python3 tools/build_skill.py --check-only
python3 tools/build_skill.py
python3 tools/grade_eval_format.py run-pilot-workspace/iteration-1
```

元数据由环境已有 Ruby/Psych `safe_load` 解析，没有为验证安装 PyYAML；
不把本检查表述为运行过缺少依赖的第三方验证器。

### 回归覆盖

- 静态识别不执行项目、`--help` 或动态配置，限制文件/字节/时间，跳过数据、权重及 symlink。
- 真正的本地短时 CPU 子进程，可靠退出、含空格/中文/shell 元字符的字面 argv。
- PID + 启动时间 + boot 标识；进程不存在、僵尸、PID 复用与采集失败分开。
- 同时客户端、跨状态目录资源登记、提交响应丢失和晚到回执，不重复启动。
- 独立入口/共享写入、批准范围/依赖、逐卡和主机余量、新任务观测、未知资源拒绝准入。
- 通知 outbox 重启续投、去重、退避/预算、模糊投递先对账、分析 spool 独立。
- 超过 1024 条历史已投递事件仍不遮住新待办；终态保留投递入口。
- 日志首次接管基线、增量、rotation/copytruncate、部分行/UTF-8、ANSI/回车进度。
- 缺未来文件不截断其他观测，失败指标不写 0；CUDA/主机/NCCL 错误分开。
- 完成报告身份、时间、启动前内容指纹、必需字段、非空分组及组内精确结果；
  `all([])`、旧报告、仅 touch 旧内容、退出 0 均不能绕过验证。
- OOM/不完整/未知完成冻结后续准入；不自动 retry/resume，不杀训练或他人进程。
- 停止监控不停止已有或托管进程，同类吞吐不佳采用 drain，异类阶段不相加。

### 无 mock CLI 闭环

`tools/verify_local_runtime.py` 在临时目录运行一个 0.25 秒 CPU fixture，
它不是科研项目的 smoke，也不是额外训练：

1. 静态检查没有执行 fixture；dry-run 没有产生 attempt 或报告。
2. 使用真实本机采集、资源门控、launcher 和 supervisor，不替换 probe。
3. 真实子进程只写小型 JSON 报告；验证 fold1/fold2、metric、subject_count=128。
4. 收到真实退出 0，执行状态为 completed，attempt=1。
5. 稳定事件文件投递 1 次，model_submissions=0，needs_scheduler=false。
6. actual_gpu=null，没有把空候选设备或 CPU fixture 冒充实际 GPU 使用。
7. 临时状态、工作进程及产物收尾，不修改实际训练项目。

### 2. 离线技能对照

文件：

- 用例与断言：`run-pilot/evals/evals.json`。
- 冻结指令快照：`run-pilot-workspace/skill-under-test/`。
- 输出、证据评分与 benchmark：`run-pilot-workspace/iteration-1/`。
- 人工评审页面：`run-pilot-workspace/iteration-1/review.html`。

三个场景分别检查新任务准入、失联/身份变化/后期 OOM、旧空报告与 outbox 重启。
每场景使用技能和不使用技能各一次。文件/JSON 部分由脚本检查，
语义部分由父任务按已列出的断言和输出证据内联评分，**不是独立盲审**。

| 配置 | 通过项 | 平均通过率 |
| --- | --- | --- |
| with_skill | 18/18 | 100% |
| without_skill | 17/18 | 94.4% |

主要分差是无技能的新任务报告缺少同类阶段总吞吐收益判据及无收益时 drain 策略。
大多数授权/不确定性条款两边都通过，提示词本身包含大量边界信息；
每配置仅一次，不能把这些数字作为普遍效果或生产可靠性证明。

子任务并发上限使第三组稍后启动。第三组两边的完整报告和 transcript 已先落盘，
但执行器之后报告限流；本任务验证了保存文件才纳入评分，不宣称执行器成功收尾。
独立代码审查也未在限流前完成。工具未返回 Token/时长，
相关指标为 unavailable/null，不使用字符数替代 Token，不比较耗时/成本。
评审页由原技能创建器的 `eval-viewer/generate_review.py` 生成，未另写自定义评审 UI。

未运行描述触发优化，也未收取/伪造用户评审反馈；页面可供后续人工复核。

### 3. 未实现与未验证分别列出

**未实现**：

- SSH 采集返回 unsupported，不会发起网络连接；远程启动与远程部署不存在。
- 自动训练 retry/resume、完整 Checkpoint 恢复、任意 validator 执行、模型 runner 不存在。
- 未安装调度服务；interval_seconds=1200 不会自行建立周期任务。
- 集群/多节点调度器、通用 DDP worker 生命周期接管、全局资源隔离不在本版。

**有代码但未实机验证**：

- Linux `/proc`、cgroup 与 NVIDIA 采集只用 fixture 检查。
- 真实 CUDA 训练、跨 GPU 并发和资源竞争，没有实机结果。
- macOS 桌面通知适配器只验证固定参数调用/失败边界，没有发送真实桌面通知或验证用户可见性。

本地事件文件投递已验证，但它不是手机推送；spool 写入不代表模型已分析。
主 PID 消失且没有可信退出时保留 unknown 登记，不能自动借用该占用；
此类未知登记需要人工对账，本版不会自动清空。

尚未选定开源许可证；即使仓库公开可读，也不代表已授予复用许可或完成独立安全审计。
