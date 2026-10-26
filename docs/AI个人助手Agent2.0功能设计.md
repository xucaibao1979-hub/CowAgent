# AI 个人助手 Agent 2.0 功能设计

## 产品判断

CowAgent 的“自进化”不训练或修改模型权重，而是持续优化四层可控资产：用户画像、长期记忆、技能和工作上下文。所有改变必须能解释、能审批、能审计、能回滚，临时网络与环境故障不得污染记忆。

## 优先级

| 能力 | 优先级 | 当前基础 | Agent 2.0 方案 |
|---|---|---|---|
| Self-improvement Review | P0 | 已有空闲会话复盘与备份回滚 | 纳入统一控制台，默认静默、最小变更 |
| 自动 User Profile | P0 | 已有个人 Agent 手动画像 | 只从用户明确或重复表达中生成候选更新 |
| 自动 Memory 提取 | P0 | 已有长期记忆、每日记忆与去重规则 | 分为事实、偏好、承诺；低风险自动、高风险审批 |
| 自动 Skill 生成 | P1 | 已有 Skill 安装、创建与工作区技能 | 重复工作流达到阈值后生成候选 Skill，不直接启用 |
| Skill Patch / Evolution | P1 | 已有受限编辑、备份及 evolution undo | 小步 Patch、差异预览、审批、版本与一键回滚 |
| Error Learning | P0 | 有日志和任务运行记录 | 通过错误指纹聚合重复失败，不学习临时环境错误 |
| Cron 定时 Agent | P0 | 已支持 Cron、间隔和单次任务 | 作为统一触发器继续复用，不再建立第二套调度器 |
| Heartbeat 主动 Agent | P1 | 缺统一主动检查 | 检查审批、异常、承诺和待办；无事项保持静默 |
| Planner + Sub-Agent | P1 | 已有 Sub-Agent 并行工具 | 增加计划、依赖、验收和汇总阶段的统一状态 |
| Human Approval + Audit | P0 | 自动化已有审批中心 | 扩展到 Memory、Profile、Skill 和外部高风险动作 |

## 核心数据

- `settings`：复盘触发方式、空闲时间、最小轮次、Heartbeat 周期、记忆/技能审批策略、错误阈值。
- `events`：操作人、时间、动作、风险、状态和关联对象，最多保留 1000 条。
- `error_patterns`：标准化错误指纹、次数、首次/最近时间、状态与经验结论。
- `proposals`：Memory、Profile、Skill Create、Skill Patch 的候选变更及审批状态。
- 真实 Memory 与 Skill 仍使用工作区文件，Agent 2.0 不复制第二份真相来源。

## 安全边界

1. 默认不学习，只有明确、重复、可复用信号才形成候选。
2. 密码、Token、验证码、健康与财务隐私不得写入长期记忆。
3. Skill 创建与 Patch 默认必须审批；内置 Skill 只读。
4. Error Learning 先聚合再判断，网络超时、缺少依赖等临时错误不写入 Memory。
5. Heartbeat 无异常时不发消息，避免主动 Agent 变成骚扰 Agent。
6. 发送、删除、写入外部系统等操作继续进入人工审批。

## 开发排期

- P0：统一控制台、策略配置、能力状态、错误聚合、审计事件、现有复盘/调度/审批接入。
- P1：Heartbeat、Profile/Memory/Skill 候选提案、技能差异与回滚、Planner 执行图。
- P2：基于用户反馈的质量评分、跨设备画像同步、团队策略与合规导出。
