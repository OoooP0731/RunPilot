# 巡检、投递与完成验证

用于调用随包脚本、核对实际监控模式、处理终态与暂停/停止。契约字段见 [contract.md](contract.md)；首次授权和准入见 [takeover.md](takeover.md)。

## 1. 本地使用与命令

保留整个 `run-pilot/` 目录（`SKILL.md`、`references/`、`scripts/`），移动或安装后以本 `SKILL.md` 所在绝对目录设置 `SKILL_DIR`，不依赖训练项目 cwd 或仓库根文档。
使用现有 Python 3 和 POSIX 环境；不为安装技能改动训练依赖、下载权重或升级驱动。
入口缺失或与随包 `--help` 不符时报告该分支不可用，不临时生成替代 launcher。

下列为命令模板，`ABS / FILE / DIR / ID / PID` 必须替换为已查证值；它们不是可直接使用的示例目标。
`DIR` 为项目外的用户私有状态目录（权限 700），各次调用使用同一目录。`ID` 在 `--execution` 和 `--unit` 位置分别表示私有执行句柄和已有单元名。

```bash
# 有界静态识别；不会运行项目入口。
python3 "$SKILL_DIR/scripts/inspect_project.py" --project ABS

# 保存已核对的契约；不启动训练。
python3 "$SKILL_DIR/scripts/run.py" init --contract FILE --state-dir DIR

# 已有进程只读接管；启动时间/boot 身份也须核对。
python3 "$SKILL_DIR/scripts/run.py" attach --state-dir DIR --execution ID --unit ID --pid PID

# 仅用于已批准、ready 且通过程序准入的本地正式单元。
python3 "$SKILL_DIR/scripts/run.py" launch --state-dir DIR --execution ID --unit ID
python3 "$SKILL_DIR/scripts/run.py" status --state-dir DIR --execution ID

# 提交响应不明时核对已有记录；不启动、重试或恢复训练。
python3 "$SKILL_DIR/scripts/run.py" reconcile --state-dir DIR --execution ID --unit ID

# 单次固定检查会保存状态并处理投递待办。
python3 "$SKILL_DIR/scripts/monitor.py" check-once --state-dir DIR --execution ID
python3 "$SKILL_DIR/scripts/monitor.py" status --state-dir DIR --execution ID
python3 "$SKILL_DIR/scripts/monitor.py" events --state-dir DIR --execution ID

# 仅改变指定单元的监控开关，不发训练停止/恢复信号。
python3 "$SKILL_DIR/scripts/run.py" pause-monitor --state-dir DIR --execution ID --unit ID
python3 "$SKILL_DIR/scripts/run.py" resume-monitor --state-dir DIR --execution ID --unit ID
python3 "$SKILL_DIR/scripts/run.py" stop-monitor --state-dir DIR --execution ID --unit ID
```

状态、事件命令不会建立周期服务；`check-once` 也不是常驻进程。
当前 `run.py status` 返回整个 execution 的单元与动作记录，不接收 `--unit`；在结果中定位单元，不把控制命令当作查询。
`--fixture FILE` 仅供 `target.kind=fixture` 的合成快照检查，不能将 fixture 当作真实启动的资源或授权证据。
`check-once --no-deliver` 会留下投递待办，不能据此报告用户已收到通知。
没有 retry、训练 resume、kill、远程 launch 或服务安装命令。

## 2. 实际监控模式

明确区分“固定程序单次检查”与“谁在定期调用它”：

| 模式 | 可以承诺的覆盖 |
| --- | --- |
| 手动单次/静态报告 | 仅已发生的检查或给定证据；没有后台巡检 |
| 固定程序 + 已核实的外部定时调用 | 按实际调用位置/间隔采集，不调用模型；休眠/关机/断网仍有缺口 |
| Agent 定时模式 | 每轮仍调用模型，不称为零模型 |

默认建议间隔 1200 秒（20 分钟）；契约中的间隔不自动创建定时器，也不是故障检测延迟保证。
零模型周期路径是“既有/另行获准的系统定时器 → `monitor.py check-once`”，不是反复提交提示词。
没有安装授权时保持手动模式并说明缺口，不默认创建 Codex 模型 cron/heartbeat。
服务安装、调用位置、日志、成本及清理方式需要单独批准；v0.1 不管理远程服务、集群 scheduler 或多节点生命周期。
SSH 采集当前明确返回 unsupported，未发起网络连接；不声明远程覆盖。

已有调度调用不因聊天窗口关闭自动等于丢失，也不因命令成功一次就等于全天在线；核实真实调用入口后报告。
v0.1 没有模型 runner；`model_submissions=0` 指固定程序自身，外部 spool 消费者若被启用，其权限与模型费用另算。

## 3. 一次检查实际做什么

1. 读取不可变契约，按 execution 加锁，有界采集仍 active 且非终态的单元。
2. 逐字段比较，SQLite 同一事务保存 runtime 与新事件/outbox；普通进度不每轮通知。
3. 释放采集锁后认领并投递 outbox，避免慢投递占用采集锁。
4. 仅在已明确批准 `schedule` 时尝试下一个 ready 单元，每轮至多一个；launcher 重新核对准入，而非直接执行模型建议。
5. 写检查记录、采集缺口、状态/事件/动作数量、`model_submissions=0` 与 `needs_scheduler`。

字段包括 process、exit、progress、errors、completion、resources、log；状态是 `ok / unavailable / not_expected_yet / parse_error`。
当前失败采集与 `last_good` 分开保留，展示最后成功值时标来源时间和 freshness，不用陈旧值准入。
某个未来阶段报告不存在只影响该字段；资源或项目日志不可解析不意味着训练已失败。
`check-once` 返回 0 表示程序检查路径成功或忙锁被处理，不是训练完成；程序错误单列并报告 `training_failure=false`。

日志按当前文件来源/generation/offset 增量处理普通文本或 JSON，关注 rotation、截断、ANSI/回车进度和未完整行。
首次 attach 的既有日志/结果建立基线；历史 traceback、旧 OOM 不当作本轮新失败。
解析不了就标缺口，不临时改训练日志格式。ETA 只用于可识别的当前同类阶段，未知总量或样本不足则 unknown。
单次 GPU 0%、无日志或 CPU 满载不足以宣布停滞；异常分类见 [recovery.md](recovery.md)。

## 4. 完成证据与退出结果

完成检查只映射项目已有 JSON reporter 产物，v0.1 不运行任意 validator 命令，也不重写科研统计。
项目已有其他 validator/reporter 可提供人工核对证据；实际执行它属于项目执行，静态检查阶段不运行。
缺少机器可验证定义不妨碍监控；不能因此新建必需 validator、分组、best weights 或修改保存逻辑。

对已映射报告同时核对：

- 真实文件读取/JSON 解析成功，必需字段存在，明确成功字段符合既定值。
- 属于当前执行和单元/attempt；项目提供归属字段时逐一匹配。
- 当前运行开始后产生或更新，且不是启动前同一文件指纹；旧目录、旧报告或仅改时间不自动证明归属。
- 项目原本有分组定义时，`expected_groups` 明确、非空且无重复，实际集合及项目要求匹配。`expected_groups=[]` 是无效契约，不能靠 `all([])` 通过，也不能删除分组约束绕过。
- 已知必需产物与阶段完整，验证失败不能被退出 0 覆盖。轻量读取即可，不反序列化不可信权重。

| 目标进程/退出 | 完成证据 | 执行结果 |
| --- | --- | --- |
| 身份匹配且存活，无冲突退出 | 任意 | running；退出仍未知 |
| 当前真实非零退出 | 即使有报告 | failed |
| 已确认目标结束 | 当前归属与完成检查都通过 | completed；若未收到退出结果，退出码仍为 unknown |
| 已确认目标结束 | 已知必需定义缺失/不通过、空组、旧报告 | incomplete |
| 已确认目标结束 | 无足够定义或归属/退出证据不足 | finished_unverified |
| 只有失联或身份不确定 | 任意旧文件 | 不推断完成；按访问/身份冲突处理 |

缺失退出文件始终只表示“未收到退出结果”。退出通道已配置不等于真正收到结果。
分别记录真实 `finished_at`、巡检 `observed_at`、`record_written_at`；真实结束时间未知时保持未知，不用发现时间冒充。
只有已批准范围全部满足完成条件才能报告范围成功；某单元完成不自动进入未批准后续阶段。

## 5. 事件、通知与分析 spool

事件按 execution/unit/attempt/类型与异常 episode 的稳定 key 去重；普通 loss/epoch 波动不是默认“新结果”。
通知与分析各有持久 outbox、认领状态和预算，进程重启不删除事件、不重置尝试次数。

| 适配器 | 实际含义 |
| --- | --- |
| notification=none | 仅本地事件；不宣称已提醒用户 |
| notification=file | 在已批准本地目录写稳定事件 JSON；不是手机推送 |
| notification=desktop | 仅 macOS 桌面；接受请求不证明用户看到，不能保证恰好一次 |
| analysis=spool | 仅写脱敏请求 JSON；没有模型执行、自动唤醒聊天或动作权限 |

外发只用必要的脱敏指标与证据；通知许可不包含完整日志或凭证外传。
文件/spool 响应丢失按稳定文件名和内容对账；桌面通知等无法确认是否送达的记录保留 unknown，不盲目重复发送。
普通投递失败在 `max_attempts` 内按退避重试；达到预算留 exhausted 和明确失败原因。unknown 保留证据并交人工核对，不视作 delivered。
`analysis.max_requests` 限制写入请求数量；写入成功只代表 spooled，不代表 analyzed。通知失败不阻塞分析 spool，分析不可用不阻塞基础巡检。

## 6. 终态、暂停/停止与状态保留

执行终态与监控开关独立。`pause-monitor`/`stop-monitor` 跳过对应采集；`resume-monitor` 恢复采集资格，不重跑训练或恢复 checkpoint。
单元终态后不再做其训练/资源采集，固定程序仍须处理既有投递待办：

1. 查看 `status` 中 outbox 及最近 `check-once` 的 `pending_delivery / needs_scheduler`。
2. 有 pending/claimed 时保留原调用入口，继续固定检查至投递成功或预算耗尽；无新事件也要处理旧待办。
3. 对 unknown 先对账或人工交接；对 exhausted 说明未送达与失败原因，保留事件/动作记录。
4. 所有投递都已成功或有明确失败/人工交接去向，且无其他 active 非终态单元需要该调用时，才结束对应周期调用。`needs_scheduler=false` 不等于所有通知已送达。

停止采集不等于可以删除 outbox，更不能撤掉唯一投递入口后声称会自动补送。
保持其他单元/执行的监控不变；用户取消训练后不自动恢复。
升级/停用技能保留私有数据库、身份、日志游标和待办；未知 schema 拒绝打开，使用兼容版本或明确迁移，不自动重建活动状态。
清理服务、状态或项目产物需独立批准；本技能不自动安装或卸载后台服务。

**最终报告**：本次范围及结果分类、实际 GPU（区分候选与已证实使用）、关键指标、并发/收益证据、失败/回退、用户指定覆盖、未知项、监控覆盖及投递/人工交接状态。
