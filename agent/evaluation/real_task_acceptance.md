# 真实任务验收案例

这份清单用于人工执行和核验真实任务。`cases.json` 的 50 条样例只自动检查路由；本机收集的真实指令需要标注并关联 `run_id` 后，才能用 `runner.py` 统计执行轨迹。不要把“路由正确”“接口接受”或“生成了回复”当成最终完成。

## 每次测试保留的证据

1. 测试编号、唯一标记（例如 `TEST-20261107-01`）、原始指令、开始和结束时间。
2. 执行 `run_id`、任务运行记录和 `execution_trace.jsonl` 中同一 `run_id` 的阶段与状态。
3. 外部系统的最终状态：接收端消息、附件名和大小、网页实际值、定时任务配置与运行记录等。截图应遮住私人信息和令牌。
4. 预期值、实际值、是否人工介入、失败原因和重试次数。没有独立的外部证据时记为“未核验”。

接口返回 `accepted` 只证明服务端接受请求。微信/企业微信消息是否到达，应以测试接收端实际看到的消息为准。当前 `runner.py` 不会读取手机截图，也不会把截图自动计入完成率；该项需要人工核验或以后接入可信回执。

## 建议先跑的 8 条案例

| 编号 | 测试指令（替换为自己的测试账号与文件） | 独立验收证据 | 通过条件 |
| --- | --- | --- | --- |
| E2E-01 微信文字 | “给我的测试微信发送 `TEST-日期-01`” | 手机测试会话截图、执行轨迹 | 收件会话正确，原文完整且只出现一次 |
| E2E-02 企业微信联系人 | “打开企业微信，给测试联系人发送 `TEST-日期-02`” | 发送前联系人姓名/部门、接收端截图 | 打开正确会话，清空旧草稿，消息只发给目标 |
| E2E-03 企业微信附件 | “把测试目录中的 `test-03.txt` 发给测试联系人” | 本地文件名/大小、接收端附件名/大小 | 附件与原文件一致，收件人正确，未附带旧草稿 |
| E2E-04 找文件 | “找出测试目录里的 `test-04.pdf`，报告完整路径和大小” | 文件管理器中的文件属性 | 路径、名称、大小一致；重名文件不会混淆 |
| E2E-05 已登录网页 | “在已经打开的测试网站标签页读取账户名称” | 原标签页、页面显示值、回答 | 复用已登录标签页，回答与页面一致，不要求重新登录 |
| E2E-06 定时提醒 | “3 分钟后给测试微信发送 `TEST-日期-06`” | 任务配置、自动运行记录、手机接收时间 | 无需手动执行，到时发送一次；失败时不报告已送达 |
| E2E-07 失败恢复 | 在测试通道临时断开后运行 `E2E-06` | 失败轨迹、补发队列和接收端 | 准确报告失败；恢复后不重复发送旧提醒 |
| E2E-08 重启恢复 | 创建 3 分钟后的测试提醒，重启本地服务 | 检查点、重启后的运行记录、接收端 | 恢复执行或明确标记待核验；接收端最多一条 |

请只用测试联系人、测试文件和测试网站执行前 8 条；失败注入与重启会影响本机服务，安排在不影响正常提醒的时段。

## 操作步骤

1. 在“自动化 → Agent 2.0 → 真实任务评测”查看场景数和已关联执行数；点击“收集本机指令”可更新私有案例。
2. 选择一条真实指令，在本机 `.cow/evaluations/real_cases.json` 中给它填写 `expected_intent`、`required_stages` 和对应执行的 `run_id`。原始指令不要改写，否则会失去与实际任务的对应关系。
3. 从 CowAgent 正常入口发出该指令或等待定时任务自然触发。记录测试接收端的实际结果。
4. 定时任务可从“自动化 → 运行记录”读取 `run_id`；对话任务可按执行时间、渠道和会话，从下方轨迹列表定位 ID。核对同一 ID 的阶段。没有运行记录的案例保持 `not_run`。
5. 运行评测命令并保存报告；人工核对外部证据后再给案例判定最终通过与否。

```powershell
cd 'D:\资料新\项目\AI Agent\CowAgent-master\CowAgent-master'
Get-Content "$env:USERPROFILE\.cow\intelligence_center\execution_trace.jsonl" | Select-Object -Last 100 | ConvertFrom-Json | Select-Object at,id,source,session_id,stage,status | Format-Table -AutoSize
.venv\Scripts\python.exe -m agent.evaluation.runner --cases "$env:USERPROFILE\.cow\evaluations\real_cases.json" --trace-root "$env:USERPROFILE\.cow\intelligence_center" --output "$env:USERPROFILE\.cow\evaluations\report.json"
```

`report.json` 中 `routing_accuracy`、`interface_accepted_count` 和 `end_to_end_success_rate` 是不同指标。当前只有具备可信设备回执的轨迹才能自动证明“设备收到”；企业微信桌面界面的发送点击和微信接口接受仍需接收端核验。
