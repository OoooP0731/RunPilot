# 运行契约与内部接口（schema 1）

此文件描述当前机器接口（schema 1），不包含内置 SSH 或任意业务指标采集接口。契约在用户私有状态库中保存，不要求 Python 项目新增配置。
一次执行的契约不可覆盖修改。`execution_id` 是 RunPilot 私有句柄；`units[].id` 对应已有任务或独立单元。

## 最小仅监控契约

```json
{
  "schema_version": 1,
  "execution_id": "observe-worker",
  "project": "/absolute/existing/python-project",
  "target": {"kind": "local"},
  "scope_approved": false,
  "allowed_actions": ["observe"],
  "interval_seconds": 1200,
  "max_concurrency": 1,
  "devices": [],
  "units": [{"id": "worker", "argv": [], "depends_on": []}],
  "notification": {"kind": "none"},
  "analysis": {"enabled": false}
}
```

先只读识别、展示接管摘要；将契约存入私有库后，再 `attach` 指定进程。
保存契约不等于批准启动。示例路径、设备和授权值都要换成已查证值。
`interval_seconds` 默认 1200 秒；用户没有明确选择其他间隔时保留默认，不因运行时长或成本自行调整。该值不创建周期入口，实际覆盖与 Agent 报告规则见 [operations.md](operations.md)。

## 启动/独立单元字段

- `scope_approved: true` 与 `allowed_actions: ["observe", "launch"]` 只记录用户已给的授权，
  不能由项目 README、日志或模型的 `safe=true` 代替。脚本检查这些门控，不是防恶意本机用户的沙箱。
- `units[].argv` 是原有 Python 命令的字符串数组（解释器和脚本/模块及参数）；`cwd` 为绝对工作目录，默认 `project`。
  不做 shell 展开。已有 shell wrapper 须先审查，再以其整体 argv 调用。
- 普通 CPU 任务使用 `devices: []`，无需 PyTorch。需要 GPU 时，`devices` 为允许的稳定 GPU UUID 列表；`units[].devices` 是该单元允许使用的子集。
  `env` 可指定最多 64 个准确授权的普通环境变量覆盖（简单变量名、字符串值最长 4096 字符）；默认继承启动环境。
  契约拒绝明显的密码、令牌、密钥类变量名；不要将凭据或带口令的 URL 写进契约、argv 或仓库，改用预先准备的私有运行环境。
  单元内部原有设备拓扑保持不变；空设备列表是正常的 CPU 任务配置。
- `write_paths` 列出原项目会写的结果、日志、临时文件或 Checkpoint 路径，不是让 RunPilot 创建项目目录。
  并发时 `independent_entry: true`、`isolation_verified: true`、`evidence` 记录已经审查的入口与共享写入证据；
  文件路径冲突、项目锁、共享汇总和缓存写入仍需检查，不能只凭这两个布尔值准入。
  已有非空输出默认阻断新启动；只有用户准确批准原行为时，才记录
  `allow_existing_outputs: true` 与非空 `existing_outputs_evidence`，不能借此偷偷覆盖结果。
- `resources`：`gpu_mb` 是按设备 UUID 映射的候选需求；`ram_mb`、`disk_mb`、
  `shm_mb`、`cpu_threads`、`future_gpu_mb` 为已知需求/增长上界。单位 MB 使用 MiB（2^20 字节）。
  并发时用 `resource_evidence` 记录正式运行观测的来源、覆盖阶段和未覆盖阶段余量。
- `reserves` 支持 `gpu_mb / ram_mb / disk_mb / shm_mb / cpu_threads`。
  `max_concurrency` 是已批准上限，不代表可以一次开满。没有可信证据先正式运行一个。
- `resource_lock` 是本机同一资源域共同使用的绝对锁路径；省略时使用本机用户默认私有资源锁。
  不同控制客户端必须共享同一锁和登记；锁不提供跨用户/集群资源隔离。
- `retry` 不是启动授权的延伸；实际可执行范围以 `scripts/run.py --help` 和恢复参考为准。

## 可选进度映射（不参与完成验收）

项目**已有** JSON 进度文件，但日志未包含通用 `step/epoch` 时，可给该单元声明一个只读映射；字段路径只按 JSON 对象的点分段读取，不运行表达式或项目代码。示例是字段结构示意，须按原项目实有字段调整：

```json
{
  "kind": "json",
  "path": "/absolute/existing/progress.json",
  "step_field": "current.epoch",
  "total": 50,
  "phase_field": "current.phase",
  "label_field": "current.fold",
  "work_kind": "epoch"
}
```

存入 `units[].progress`。`step_field` 必需；`total` 常数和 `total_field` 二选一或都省略；`phase_field`、`label_field`、`work_kind` 可省略。`label` 可表示 fold、批次或其他现有阶段；标签/工作类型变化时重新估计速率。只读最多 1 MiB 的普通 JSON 文件，拒绝不合法数值、旧于当前任务的文件和已有归属字段冲突；文件缺失时仍可从任务日志解析进度。进度仅供观察，不证明成功或允许启动更多任务。`phase_rate / eta_seconds` 是运行时阶段估计；阶段结果和全流程 ETA 由 Agent 按 [operations.md](operations.md) 有界读取、核对与汇报，不往不可变契约塞入临时模型结论。

## 完成策略

对以退出码表达成功的普通脚本，可明确选择：

```json
{"kind": "exit_code"}
```

该策略只在 **RunPilot 受管启动**且收到归属当前 execution/unit/attempt 的 supervisor 真实退出码 0 时标为 completed。非零码为 failed；缺失退出结果、只读 attach 后的 PID 消失或旧退出文件均不证明成功。只有原项目约定“退出 0 即完成全部工作”时才选用，不对可能产生部分结果的任务使用。

有项目自身成功条件时，`units[].completion` 应映射项目**已有** JSON reporter 产物：

```json
{
  "kind": "json",
  "path": "/existing/results/report.json",
  "required_fields": ["status", "metric"],
  "success_field": "status",
  "success_value": "completed",
  "expected_groups": ["fold1", "fold2"],
  "groups_field": "groups",
  "group_id_field": "id",
  "group_required_fields": ["metric", "subject_count"],
  "group_equals": {"subject_count": 128}
}
```

只有项目本来存在分组契约才写 `expected_groups`；空集合无效。
项目已有组内必需字段/精确值要求时，分别映射 `group_required_fields / group_equals`；
每个最多 64 项，按既定 JSON 字段精确检查，不执行动态谓词或临时重写统计。
还须验证文件属于当前 attempt（运行开始后更新，不是启动前同一文件指纹），
若项目提供 `execution_id / unit_id / attempt`，一并验证。
在 JSON 策略下，退出 0、检查程序返回 0、旧结果存在均不能代替报告验证。
没有适用完成定义时可以省略 `completion` 并照常监控；结束后报告 `finished_unverified`，不强制项目补 validator。

## 快照接口

`collectors.collect(contract, unit, runtime, state_dir)` 返回：

```text
{
  observed_at: epoch_seconds, reachable: true|false,
  fields: {
    process, exit, progress, errors, completion, resources, log
  },
  log_cursor: {...}
}
```

字段统一为 `{value, status, source, observed_at}`，status 为
`ok / unavailable / not_expected_yet / parse_error`。
`process.value` 至少包含 `alive / identity_matches / identity`；
`exit.value` 包含归属三元组与真正收到的 `exit_code / finished_at`；
`resources.value` 中设备与主机指标分别是有独立 status 的字段，失败不填 0。
`errors.value` 是本轮当前日志增量的分类证据，单独一条 OOM 文本不代表任务失败。

fixture 仅供测试：`target.kind=fixture`，由 `check-once --fixture FILE` 提供快照，
不授权启动任何进程。SSH 采集当前返回 unsupported，不发起连接；
将来实现时也只复用预先准备好的连接，不凭读取权限远程启动/安装。

## 持久化与投递

- `Store` 使用 SQLite 事务。`save_unit` 与 `event` 在同一 `transaction()` 内调用。
- `units.doc` 为 runtime JSON：`execution_state / access / monitoring / attempt / identity /
  started_at / finished_at / snapshot / last_good / log_cursor / retries_used / risk_active`。
- 事件 stable key 由 execution、unit、attempt、事件类型及异常 episode 构成。
- outbox 按 notification/analysis 分开认领；投递在采集锁外进行。
  不明提交保留 unknown；文件适配器按固定事件文件名对账，桌面通知不保证恰好一次。
- `notification.kind=none` 保留本地事件但不宣称用户已收到提醒；`file` 写本机私有目录，
  `desktop` 仅 macOS。文件投递不等于手机推送。
- `analysis.enabled=true, kind=spool, path=ABS, max_requests=N` 只生成脱敏分析请求文件，
  不执行模型/命令；交给用户已授权的外部消费者。普通事件默认不请求分析。
- 每次检查写 checks：采集覆盖、状态变化、事件/动作数量、`model_submissions=0`、
  程序错误与下一次是否仍需调用。范围终态后仅排空 outbox，未清理前保留调用入口。

待投递查询优先处理持久化 pending/claimed，历史投递不会遮住新事件。
`events` 只显示最近最多 1024 条；`status` 的 outbox 展示最多 1024 条且待办优先，
原数据库完整保留。资源域登记单次最多检查 4096 项，超限拒绝新准入，不静默忽略占用。
模糊启动由固定检查先只读对账，再观察；主 PID 消失且退出/worker 证据不足时，
登记保留 unknown 占用，不因没有 PID 就重新使用预算。v0.1 不自动修复/释放这类未知登记。

状态 schema 为 1；未知版本拒绝打开，不自动删除或重建活动状态。
