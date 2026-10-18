# RunPilot 测试与验证

## v0.1.3 — 2026-10-18

- macOS / Python 3.9.6 与 3.12.14 各运行 **135 项 unittest，全部通过**；新增现有 JSON 进度映射、旧文件/归属冲突、阶段切换速率重置、默认串行以及受管退出采样竞态的回归。
- 两版本的无 mock 本地 CPU CLI 闭环通过：普通脚本和已有 JSON 报告示例均得到真实退出 0、`completed` 及完成事件投递；固定程序内 `model_submissions=0`。3.12.14 的闭环额外连续复跑 6 次，仅有完成通知；这不统计外部 Agent/heartbeat 的 token 用量。
- `npx skills --help` 核对存在指定技能 `update [skills...]` 与 `-g/-p` 范围选项；`npx skills add . --list` 识别 `run-pilot`。**未对用户已安装的技能执行更新**。更新模块是按需操作流程，不会自动追踪或安装 GitHub 新版。
- 14 文件安装包经过 YAML/内部引用/源码一致性、ZIP CRC 与 SHA-256 校验，`run-pilot/references/update.md` 已包含在包内。未在 Linux 实机、Windows 原生、真实 GPU/SSH、实际调度器或自动唤醒 Agent 的环境中验证；SSH/远程启动与自动调度安装仍未实现。
