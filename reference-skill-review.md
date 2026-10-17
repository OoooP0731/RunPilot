# RunPilot 参考 Skill 核查摘要

核查日期：2026-10-15（Asia/Shanghai）

本文对照用户指定的目录页与第一方源码，区分实际文件、静态推断和 RunPilot 建议。未执行上游程序、训练、服务器配置或定时任务；不是运行通过证明或完整安全审计。

## 1. 版本与范围

| 仓库 | 本次固定快照 | 读取的核心文件 |
| --- | --- | --- |
| wanshuiyin/Auto-claude-code-research-in-sleep | `26b95cfa0d8747078e9e43b42e20952709e561b8` | 常规版、Codex 版 training-check，仓库级 watchdog，以及根 LICENSE |
| openclaw/openclaw | `8b15f79f4f4467b8c4cf520fe95b46e9eb309284` | tmux Skill、两个 helper，以及根 LICENSE |
| BagelHole/DevOps-Security-Agent-Skills | `0365f57a079b1332f95cf26e31dd2d5332a8399f` | GPU Skill 及根 LICENSE |

固定 SHA 只表示已读内容，不表示独立发行号、推荐版本或安装工具必然选择的路径。training-check 有同名双路径；目录页没有可核实的索引 SHA，不能把它与当前上游常规版混为一谈。[D-T][T-MAIN][T-CODEX]

## 2. training-check：适合借鉴证据，不继承科研裁决权

**可确认事实：** 常规版指导 agent 读取 WandB/SSH 指标并判断继续或停止，自动调整 10–60 分钟调度；Codex 版为 30 分钟交互检查。[T-MAIN][T-CODEX] 仓库级 watchdog 才是无模型的 Python 状态检查；它观察会话、GPU 等并输出机器状态，不实现 PyTorch OOM 恢复。[T-WATCH]

**RunPilot 建议：** 借鉴“观测不足先保留不确定性”和检测/解释分开。不要照搬按科研指标自动停训、未经确认创建调度、指定模型/MCP 或强制项目笔记。仅在模糊情况额外调用 reviewer，不等于主 agent 的正常周期零调用。

**静态边界：** watchdog 的 GPU 查询失败可能被后续会话存活判断掩盖；session 存在也不证明目标训练子进程健康。这些是控制流风险，未作实机复现。[T-WATCH] RunPilot 应把采集失败单列未知，并以目标进程身份和多轮证据判断。

watchdog 在仓库级 `tools/`，不能假定只安装 Skill 子目录就会获得它。

## 3. OpenClaw tmux：可选终端适配，不是训练 supervisor

**可确认事实：** Skill 覆盖 session/pane 查找、输出捕获及输入；一次性命令和新非交互后台任务可走其他执行方式。[M-SKILL] find helper 支持指定 socket；wait helper 默认短时循环匹配 pane 文本，不是等待训练进程退出。[M-FIND][M-WAIT]

**RunPilot 建议：** 借鉴精确目标定位、字面文本输入和防回显误命中的标记方式。保留原有启动方式；不强迫所有任务迁入 tmux，不自动发送肯定输入批准权限，不把发现 session 理解为获得控制权。

**静态边界：** 两个 helper 的 socket 接口不对称；find 的帮助描述与实际 grep 匹配语义也有差别。[M-FIND][M-WAIT] 适配时需要自己的目标一致性测试。pane 文本出现完成标记不能证明训练退出成功或产物完整。

## 4. gpu-server-management：观测清单，不是安全自适应实现

**可确认事实：** 所读 Skill 是命令示例文档，混合 GPU 查询、驱动安装、功耗/ECC、MIG 和系统配置；不是通用 PyTorch 接管或 OOM 恢复程序。[G-SKILL]

**RunPilot 建议：** 只参考轻量只读字段；管理员配置不进入默认路径。现有科研任务的接管不能顺带改宿主环境、设备可靠性、分区、网络暴露或其他用户资源。文档中的固定版本和功耗值不应被当作实机适配规则。

**静态边界：** MIG 示例中的实例数量与同页自己的容量说明冲突。[G-SKILL] 不执行这些示例，也不把目录页标志当作硬件兼容性认证。

## 5. 发布与许可核对

固定快照的三个根 LICENSE 均为 MIT；原文版权主体分别为：

- `2026 wanshuiyin`。[T-LICENSE]
- `2026 OpenClaw Foundation`。[M-LICENSE]
- `2026 Toby Miller`。[G-LICENSE]

三份许可均包含保留版权及许可告示的条件。发布若复制或实质性改编相应材料，应按实际使用的许可文本处理，不只放一个目录链接；注明源路径、SHA 和改动有助于追溯。仅借鉴思路与复制具体表达/脚本应明确区分，不暗示上游背书。

这不是完整法务或供应链结论：tmux、WandB、NVIDIA 软件、镜像及其他依赖的条件没有被仓库根 MIT 自动覆盖；本次也未穷尽贡献历史、商标及全部依赖。具体再分发范围需另行核对。

## 6. 对 RunPilot 的收敛结论

建议的参考组合：**training-check 的证据与不确定性原则 + watchdog 的无模型检测思路 + tmux 的精确终端目标 + GPU 文档的必要观测字段。**

三个来源都不能替代 RunPilot 自己的能力门控、参数保护、持久状态、有限 OOM 重试、失联判定和完成验证。不整包导入其权限或管理员操作。

首版应优先验证：原配置启动/接管、状态正确、无模型巡检、事件通知、结果诚实验证；独立单元并行只开放给已有证据的项目入口。完整产品边界见同目录 `RunPilot-plan-v0.4.md`；2026-10-16 用户反馈对应的并行与监控契约、待实现验收见 `RunPilot-runtime-design.md`。本文的上游源码核查日期不变。

## 7. 来源与核验限制

以下为目录入口与固定源码，文件路径可在链接中直接复核。OpenClaw Contents API 曾限流，因此仅声明已读文件，不声称穷尽目录；其余访问故障未被当作文件不存在。未连接训练机、GPU、WandB 或实际部署调度。

- [D-T] training-check 目录：`https://skills.sh/wanshuiyin/auto-claude-code-research-in-sleep/training-check`
- [D-M] tmux 目录：`https://skills.sh/openclaw/openclaw/tmux`
- [D-G] GPU 目录可访问入口：`https://www.skills.sh/bagelhole/devops-security-agent-skills/gpu-server-management`
- [T-MAIN] 常规版：`https://github.com/wanshuiyin/Auto-claude-code-research-in-sleep/blob/26b95cfa0d8747078e9e43b42e20952709e561b8/skills/training-check/SKILL.md`
- [T-CODEX] Codex 版：`https://github.com/wanshuiyin/Auto-claude-code-research-in-sleep/blob/26b95cfa0d8747078e9e43b42e20952709e561b8/skills/skills-codex/training-check/SKILL.md`
- [T-WATCH] watchdog：`https://github.com/wanshuiyin/Auto-claude-code-research-in-sleep/blob/26b95cfa0d8747078e9e43b42e20952709e561b8/tools/watchdog.py`
- [M-SKILL] tmux Skill：`https://github.com/openclaw/openclaw/blob/8b15f79f4f4467b8c4cf520fe95b46e9eb309284/skills/tmux/SKILL.md`
- [M-FIND] find helper：`https://github.com/openclaw/openclaw/blob/8b15f79f4f4467b8c4cf520fe95b46e9eb309284/skills/tmux/scripts/find-sessions.sh`
- [M-WAIT] wait helper：`https://github.com/openclaw/openclaw/blob/8b15f79f4f4467b8c4cf520fe95b46e9eb309284/skills/tmux/scripts/wait-for-text.sh`
- [G-SKILL] GPU Skill：`https://github.com/BagelHole/DevOps-Security-Agent-Skills/blob/0365f57a079b1332f95cf26e31dd2d5332a8399f/infrastructure/servers/gpu-server-management/SKILL.md`
- [T-LICENSE] training-check 仓库许可：`https://github.com/wanshuiyin/Auto-claude-code-research-in-sleep/blob/26b95cfa0d8747078e9e43b42e20952709e561b8/LICENSE`
- [M-LICENSE] OpenClaw 仓库许可：`https://github.com/openclaw/openclaw/blob/8b15f79f4f4467b8c4cf520fe95b46e9eb309284/LICENSE`
- [G-LICENSE] GPU 仓库许可：`https://github.com/BagelHole/DevOps-Security-Agent-Skills/blob/0365f57a079b1332f95cf26e31dd2d5332a8399f/LICENSE`
