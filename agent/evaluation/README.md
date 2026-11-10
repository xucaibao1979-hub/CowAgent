# 任务评测

`cases.json` 是 50 条可公开提交的场景回归样例，来自本项目常见操作需求。运行：

每条微信指令现在有独立的 `run_id` 和本机任务状态文件，位于
`~/.cow/intelligence_center/task_states/`。发送超时或接口空响应记为
`awaiting_verification`；恢复时先核对接收端，不自动重复发送。

```powershell
.venv\Scripts\python.exe -m agent.evaluation.runner --cases agent/evaluation/cases.json
```

收集本机历史会话中的实际指令：

```powershell
.venv\Scripts\python.exe -m agent.evaluation.collect --limit 100
```

实际指令默认写入用户目录 `.cow/evaluations/real_cases.json`，不进入仓库。它们初始为 `unreviewed`，需人工标注 `expected_intent` 和必要的 `required_stages`；没有标注的不计入路由准确率。把真实执行的 `run_id` 填入对应案例后，可使用 `--trace-root` 复查执行结果。没有执行记录的案例始终是 `not_run`，接口接受与设备确认收到分别统计。

```powershell
.venv\Scripts\python.exe -m agent.evaluation.runner --cases "$HOME\.cow\evaluations\real_cases.json" --trace-root "$HOME\.cow\intelligence_center"
```

评测不会重新发送微信、邮件、附件，不会操作桌面或网页；它只评估路由及已有轨迹。完整外部任务需要在隔离测试账号中运行并记录接收端回执。

在隔离测试账号实际收到消息后，测试人员可以把回执写入
`~/.cow/evaluations/verified_receipts.json`。例如：

```json
[{"run_id":"对应的执行 ID","status":"received","evidence_type":"test_phone_screenshot","evidence_ref":"sha256:截图摘要","verified_by":"测试人员"}]
```

运行评测时附加 `--receipts` 路径；新任务可加
`--task-state-root "$HOME\.cow\intelligence_center\task_states"`，按同一会话和
完全相同的指令自动关联 `run_id`。重复指令无法唯一匹配时仍需人工指定。
回执只能关联已有执行轨迹，不能凭空制造
一次成功运行。报告分别列出接口接受、待核验、真实接收、误报成功和相同内容
重复发送的次数。回执需要测试人员核对接收端；这一步不能由发送接口代替。
