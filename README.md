# RunPilot

接管已有 PyTorch 任务，在保留项目配置的基础上安排运行、观察状态，并只在有意义的事件发生时通知或请求 Agent 分析。

## 当前交付：v0.1.0

仓库现提供独立的 [run-pilot/SKILL.md](run-pilot/SKILL.md)、按需参考文档、
Python 标准库运行时和合成/CPU 回归测试。固定程序不是每轮读取提示词的 Agent。
**复制技能或保存契约都不会自动安装服务、连接训练机或启动训练。**

这是保守的首版运行闭环，不是“任意项目全自动接管”：

| 能力 | 本版边界 |
| --- | --- |
| 项目识别 | 有界静态检查，不 import 项目、不执行训练入口/`--help` |
| 本地启动与接管 | 结构化 argv；本地 supervisor 保存真实退出结果；已有进程只读接管、不迁移启动器 |
| 固定程序巡检 | SQLite 状态/事件/outbox 事务；执行、可达性、监控开关分开；没有内置模型调用 |
| 独立单元准入 | 在原有入口、批准范围、写入隔离及资源证据充分时逐个增加；风险下 drain，不杀在跑任务 |
| 完成判定 | 消费映射好的项目已有 JSON 报告；归属、必需字段、非空分组和新旧产物检查；证据不足不报成功 |
| 通知 | 本地幂等事件文件；macOS 桌面适配器。不宣称文件通知等于手机推送，桌面接收不等于用户已读 |
| 可选分析 | 仅写本地脱敏请求 spool；不执行任意命令、不自动唤醒模型或聊天窗口 |
| 自动恢复 | 本版关闭；不因 OOM、best weights 或退出文件缺失自动重跑 |
| SSH / 调度器 | SSH 采集接口当前明确返回 unsupported，不发起连接；不提供远程启动、后台服务自动安装或集群资源后端 |

**程序本身不调用模型，不等于已经部署了持续监控。** 默认建议间隔为 20 分钟；
本轮没有安装任何周期任务。没有独立调度入口时只能手动执行或如实声明 Agent 模式，
不能把 Agent 的静默回复当成零模型巡检。

## 安装体与依赖

```text
run-pilot/
├── SKILL.md
├── references/       # 接管、契约、运行与恢复边界
├── scripts/          # inspect_project.py / run.py / monitor.py 与共享模块
└── evals/            # 开发用行为评测，不打入安装归档
tests/                # 标准库回归测试；不含科研数据或权重
tools/build_skill.py  # 元数据/引用/语法检查与可复现打包
```

运行时要求 Python 3.9+、POSIX 文件锁及 SQLite；不会安装/升级 PyTorch 或 CUDA。
打包校验使用环境已有的 PyYAML，或已有 Ruby/Psych 的安全 YAML 解析器，均不自动安装。
`dist/run-pilot.skill` 为 ZIP 格式安装归档，另附 SHA-256 文件；也可直接使用源码目录。

将整个 `run-pilot/` 目录放入宿主支持的技能目录，保留 scripts 与 references。
先确认目标没有同名技能，避免覆盖本地修改。本次交付不擅自写入全局技能目录。

## 快速开始（仅监控）

以下路径为示例，先换成真实的技能目录、项目路径、私有句柄及 PID。
只读监控不需要 `scope_approved=true`，更不包含停止/重启训练的权限。

```sh
SKILL_DIR="/absolute/path/to/run-pilot"
STATE_DIR="$HOME/.local/state/run-pilot"

python3 "$SKILL_DIR/scripts/inspect_project.py" --project "/absolute/existing/project"

# 根据已查证信息准备私有契约；格式见 references/contract.md。
python3 "$SKILL_DIR/scripts/run.py" init \
  --contract "/absolute/private/observe-contract.json" --state-dir "$STATE_DIR"
python3 "$SKILL_DIR/scripts/run.py" attach \
  --execution "observe-existing" --unit "existing" --pid 12345 --state-dir "$STATE_DIR"
python3 "$SKILL_DIR/scripts/monitor.py" check-once \
  --execution "observe-existing" --state-dir "$STATE_DIR"
python3 "$SKILL_DIR/scripts/monitor.py" status \
  --execution "observe-existing" --state-dir "$STATE_DIR"
python3 "$SKILL_DIR/scripts/monitor.py" events \
  --execution "observe-existing" --state-dir "$STATE_DIR"
```

契约模板和字段含义见 [references/contract.md](run-pilot/references/contract.md)。
契约保存在 RunPilot 私有状态，不要求训练项目新增交接文件。
启动命令与资源上限必须来自用户授权，不能把示例中的批准值照抄为真实授权。
`launch --dry-run` 只做准入检查；不改变单任务配置。

`check-once` 输出的是检查记录，退出 0 不表示训练成功。主要输出字段：

- `model_submissions`：本程序直接提交模型的次数，固定为 0；spool 请求另行计数。
- `program_errors`：监控器自身错误，不伪造训练失败。
- `needs_scheduler`：还有活动监控或投递待办时为 true；false 才表示可撤掉对应调用入口。
- `pending_delivery`：终态后通知失败仍保留待办，不因停止训练采集而丢掉最后一次投递。

JSON API、日志/完成报告解析都有范围限制；无法解析时保留未知，不保证从任意自由文本推导科研完成。

## 停止、升级与卸载

```sh
python3 "$SKILL_DIR/scripts/run.py" stop-monitor \
  --execution "observe-existing" --state-dir "$STATE_DIR"
```

**停止监控不停止训练。** 需要继续投递的 outbox 仍应保留固定程序调用入口。
暂停/恢复仅切换监控开关；已有终态不会被自动复活。

- 更新或移走技能目录时保留私有状态与资源域登记；不要为升级清空活动运行状态。
- SQLite 状态 schema 为 1；未知 schema 拒绝打开，不悄悄重建。
- 本版不自动安装调度器，所以没有隐藏服务需要卸载。若用户自行配置了调用入口，
  先核实所有对应单元与通知待办，再单独停止该入口，不影响其他任务。
- 卸载只移除安装的技能目录；运行记录、通知和训练产物由用户决定是否保留。
- 不删除仍被 supervisor 使用的脚本、私有 attempt 目录或资源登记。

## 开发与验证

```sh
make test       # stdlib unittest；仅合成 fixture 与短时 CPU 子进程
make validate   # YAML、内部引用与 Python 语法检查
make build      # 先测试/校验，再创建 dist/run-pilot.skill
```

真实 NVIDIA GPU、跨 GPU 并发与桌面通知权限需在目标环境单独验证；
SSH、集群调度器及自动恢复则尚未实现，不能将它们仅描述为“未实测”。
不能用本机 CPU 测试冒充 GPU 支持认证。121 项双 Python 版本回归、本地 CPU 闭环和
离线对照结果见 [TESTING.md](TESTING.md)；全部结果与局限均有记录。
公开发布许可证尚待维护者选择；公开可读不等于授予复用许可。本次未复制参考仓库的脚本或许可文本，也不暗示上游背书。

## 运行边界

- 科研配置由项目和用户提供；优先通过已批准独立任务的并行执行提高整体效率，不为占满显存调参。
- 原任务没有 smoke 时不自行创建；正式任务的自然运行可以提供资源观测。
- 允许跨 GPU 运行；报告本次实际使用的 GPU。
- 正常巡检的目标路径是“定时器 → 固定程序 → 状态比较 → 无事件退出”，只有必要事件进入 Agent。独立程序部署前不宣称已经零模型运行。
- 监控不等于取得停止/重启权限，设计文档不等于批准部署。

## SSH 连接准备（发布说明）

远程使用时，由用户预先准备可用的 SSH host 配置和认证。无人值守检查需要能够在不等待交互输入的情况下连接；复用已有连接方式，不由 RunPilot 每轮讨论密码或创建专用监控密钥。

账户权限、密钥保管和访问限制由用户管理；本 Skill 不负责建立“只读监控密钥”体系。保留主机身份校验、连接/命令超时；认证失败时说明连接尚未就绪，不自行变更服务器认证设置。

不要将密码、私钥或令牌粘贴进公开问题、示例、训练日志或版本库。发布用 fixture 应使用虚构连接信息。

## 设计依据与后续范围

- [RunPilot-plan-v0.4.md](RunPilot-plan-v0.4.md)：产品范围与用户决策；保留原始设计状态。
- [RunPilot-runtime-design.md](RunPilot-runtime-design.md)：并行、事件/动作契约与完整目标验收。
- [RunPilot-skill-prompt.md](RunPilot-skill-prompt.md)：原始提示词；当前安装入口为技能目录中的 SKILL.md。
- 历史试用复盘及原始评测工作区保留在本地，不随公开仓库发布；其中含本机路径和执行记录。
- [reference-skill-review.md](reference-skill-review.md)：此前对参考 Skill 固定版本的核查。
- [CHANGELOG.md](CHANGELOG.md)：实现版本与已明确收敛的范围。

后续再按授权接入常在线训练端调度、经实机验证的 GPU/远程适配和项目特定恢复。
原始设计文档中的未来能力不自动成为本版已实现或已部署的承诺。
