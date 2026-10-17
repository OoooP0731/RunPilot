# RunPilot

**接管已有 Python 任务，清楚地知道它在运行、结束、失败，还是暂时无法确认。**

RunPilot 适用于数据导出、ETL、批处理、长运行 worker、服务脚本及机器学习训练等 Python 工作负载。它保留项目原有命令和配置：先只读识别，再按授权启动新任务或接管本地现有进程；固定程序按次巡检进程、日志与资源，记录事件并投递通知。多个原本独立的任务可以在写入隔离、资源和授权证据充分时逐个准入，而不是盲目开满并发。

RunPilot **不要求 PyTorch，也不要求 GPU**。它不替项目发明启动参数、成功标准或恢复策略；没有可靠证据时，结果会保持未验证。

## 能做什么

- **识别与接管**：有界静态检查 Python 项目，不导入或执行项目代码；已有本地进程按 PID、启动时间与系统标识只读接管，不停止或重启它。
- **安全启动**：获准后以原始 argv 和工作目录启动本地 Python 任务；启动前检查资源余量、重复提交、依赖和输出路径。受管任务通过 supervisor 记录真实退出码。
- **按次巡检**：检查进程、CPU/内存/磁盘（有设备时也检查 GPU）、增量日志、退出和完成证据；将状态、事件与通知待办保存在 SQLite。识别 `step/epoch` 及 `current/processed/progress` 等有限格式，未解析的日志不被误判为停滞。
- **自选完成条件**：普通受管脚本可明确采用退出码 0；已有 JSON 报告的任务可映射必需字段、成功值和分组。两者不能混用来绕过项目的真实验收标准。
- **独立单元调度与通知**：在证据充分时逐个准入；资源或吞吐风险下停止增加新任务、让已运行任务自然结束。事件可写本地文件或发送 macOS 桌面通知；可选分析只写脱敏的本地请求文件，不自行调用模型。

## 安装

要求 Python **3.9+**、Linux/macOS、本机 POSIX 文件锁和 SQLite；运行时只使用 Python 标准库。下载 [v0.1.1 安装包](https://github.com/OoooP0731/RunPilot/releases/tag/v0.1.1) 的 `run-pilot.skill` 及同名 `.sha256`，校验后将 ZIP 解压至技能宿主的目录；也可直接使用仓库中的 [`run-pilot/`](run-pilot/)。归档自带 `SKILL.md`、`references/` 与 `scripts/`，请完整保留。安装前检查目标位置是否已有同名技能，避免覆盖。

```sh
shasum -a 256 -c run-pilot.skill.sha256
unzip run-pilot.skill -d /path/to/your/skills
```

安装技能不等于配置常驻巡检、授权启动、连接远程主机或运行项目。下述命令可直接使用源码/解包后的脚本；`SKILL_DIR` 指向包含 `SKILL.md` 的**绝对路径**。

## 快速开始：管理一个普通 Python 脚本

先确认项目原命令、工作目录、输出位置、资源需求，以及“退出码 0 是否真的意味着任务完成”。示例中的授权字段仅用于演示；真实使用须由任务所有者明确批准，不能照抄示例授予权限。

```sh
SKILL_DIR="/absolute/path/to/run-pilot"
STATE_DIR="$HOME/.local/state/run-pilot"
python3 "$SKILL_DIR/scripts/inspect_project.py" --project "/absolute/my-project"
# 已知原命令时还可传入 --command-json '["/absolute/path/to/python3","/absolute/my-project/custom.py"]'
```

把下面的 JSON 存为**项目目录之外的私有文件** `/absolute/private/export-contract.json`，将路径、解释器、资源估算和授权替换成实际值：

```json
{
  "schema_version": 1,
  "execution_id": "daily-export",
  "project": "/absolute/my-project",
  "target": {"kind": "local"},
  "scope_approved": true,
  "allowed_actions": ["observe", "launch"],
  "max_concurrency": 1,
  "devices": [],
  "reserves": {"ram_mb": 512, "disk_mb": 1024, "cpu_threads": 1},
  "units": [{
    "id": "export",
    "argv": ["/absolute/path/to/python3", "/absolute/my-project/export.py"],
    "cwd": "/absolute/my-project",
    "write_paths": ["/absolute/my-project/output.csv"],
    "resources": {"ram_mb": 512, "disk_mb": 512, "cpu_threads": 1},
    "completion": {"kind": "exit_code"}
  }],
  "notification": {"kind": "none"}
}
```

`completion: {"kind":"exit_code"}` **只适用于 RunPilot 受管启动，且仅在项目约定成功退出即完成时使用**。如果脚本可能在输出不完整时仍返回 0，应改用项目已有的 JSON 完成报告，或不指定完成条件；不要把“进程退出”误报为业务成功。`write_paths` 声明原任务会写的路径，RunPilot 不会替项目创建输出目录。现有非空输出默认阻断新启动。
如需原任务的非凭据环境覆盖，可在单元内指定 `env`（例如 `{"JOB_MODE":"export"}`）；默认继承已准备的运行环境，切勿在契约或命令参数中保存密码、令牌和带口令的 URL。

```sh
python3 "$SKILL_DIR/scripts/run.py" init --contract /absolute/private/export-contract.json --state-dir "$STATE_DIR"
python3 "$SKILL_DIR/scripts/run.py" launch --execution daily-export --unit export --dry-run --state-dir "$STATE_DIR"
python3 "$SKILL_DIR/scripts/run.py" launch --execution daily-export --unit export --state-dir "$STATE_DIR"
python3 "$SKILL_DIR/scripts/monitor.py" check-once --execution daily-export --state-dir "$STATE_DIR"
python3 "$SKILL_DIR/scripts/monitor.py" status --execution daily-export --state-dir "$STATE_DIR"
python3 "$SKILL_DIR/scripts/monitor.py" events --execution daily-export --state-dir "$STATE_DIR"
```

`init` 保存不可变的执行契约但**不会启动**；`--dry-run` 只检查准入。请在提交前审查原脚本的副作用和真实资源需求。`check-once` 只巡检一次，退出状态 0 表示检查程序成功，不代表任务完成；查看 `status.units.<id>.execution_state` 和退出/完成证据。

### 已经在运行的 Python 进程

仅观察时，把契约改为 `"scope_approved": false`、`"allowed_actions": ["observe"]`，为单元提供 `"argv": []`，保存后执行：

```sh
python3 "$SKILL_DIR/scripts/run.py" init --contract /absolute/private/observe-contract.json --state-dir "$STATE_DIR"
python3 "$SKILL_DIR/scripts/run.py" attach --execution existing-job --unit worker --pid 12345 --state-dir "$STATE_DIR"
python3 "$SKILL_DIR/scripts/monitor.py" check-once --execution existing-job --state-dir "$STATE_DIR"
```

示例中的 execution/unit 必须与观察契约一致。接管不会变成父进程，也不能凭 PID 消失获取可信退出码；即便写了 `exit_code` 完成策略，只读接管仍不会据此判成功。需要业务完成证据时，映射项目**已经存在**的 JSON 报告；格式和归属限制见 [运行契约](run-pilot/references/contract.md)。

## 持续监控、状态和通知

要持续观察，可在**自行批准并配置的**系统定时器/已有调度入口中定期调用同一个 `check-once`（例如 20 分钟一次）。RunPilot 不会自动安装后台服务；没有外部调用时只是手动巡检。正常巡检由固定程序执行，不需要每次唤醒 Agent。`needs_scheduler` 表示仍有活动单元或待投递事项；终态后也不要在通知待办处理前撤掉唯一调用入口。

- `running`：当前进程身份匹配，正在运行；长运行服务可以一直保持此状态。
- `completed`：按**所选策略**验证完成：受管进程真实退出 0，或当前执行的 JSON 报告符合约定且任务已结束。
- `failed`：已收到当前任务的真实非零退出；`incomplete`：已知报告验收不通过；`finished_unverified`：进程已结束，但缺少可信退出/完成定义。
- `access`、`monitoring` 与 `execution_state` 分开记录。失联、无日志或检查器报错不自动等同于任务失败。

`pause-monitor` / `resume-monitor` / `stop-monitor` 控制采集开关，**不会停止或重启 Python 进程**。文件通知留在本机，桌面通知仅适用于 macOS；均不等于手机推送。分析 spool 写入不代表已有模型完成分析。

## 支持范围与约束

当前可在本地启动/接管 Python 任务、按次巡检、按证据准入独立任务及投递本地事件；GPU 是可选资源，真实 NVIDIA GPU 与跨卡并发尚未实机验证。**SSH 采集/远程启动、集群调度器、自动 retry/resume、任意验证脚本和自动安装周期服务尚未实现**。不为了诊断任务而运行未经批准的项目代码，也不因 OOM 自动重跑或改配置。

RunPilot 不保证所有 Python 项目都能无修改地取得业务级成功判定、可靠进度或自动并发：这些能力取决于项目原有入口、输出、日志与授权。完整字段和安全边界见 [接管](run-pilot/references/takeover.md)、[运行契约](run-pilot/references/contract.md)、[巡检](run-pilot/references/operations.md) 和 [异常交接](run-pilot/references/recovery.md)。

本仓库未选定开源许可证；公开可读**不等于**获得复用授权。

## 开发与验证

```sh
make test
make validate
make build
python3 tools/verify_local_runtime.py
```

版本变化见 [CHANGELOG.md](CHANGELOG.md)，实测环境与未覆盖范围见 [TESTING.md](TESTING.md)。仓库内早期的 `RunPilot-plan-v0.4.md` 等是历史设计稿，并非当前发行能力承诺。
