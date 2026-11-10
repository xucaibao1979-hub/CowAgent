from agent.execution.task_state import TaskStateStore


def test_independent_runs_and_uncertain_send_are_not_retried(tmp_path):
    store = TaskStateStore(tmp_path)
    first = "weixin-11111111"
    second = "weixin-22222222"
    store.begin(first, goal="发送正面n.jpg", source="weixin", session_id="same-user")
    store.begin(second, goal="查询天气", source="weixin", session_id="same-user")
    store.record({"id": first, "stage": "微信接口发送", "status": "unconfirmed", "at": "2026-11-07T16:00:00+08:00"})
    one = store.read(first)
    two = store.read(second)
    assert one["goal"] == "发送正面n.jpg"
    assert one["status"] == "awaiting_verification"
    assert store.recovery_plan(one) == "verify_before_retry"
    assert two["goal"] == "查询天气"
    assert two["delivery"] == "not_started"
    store.record({"id": first, "stage": "设备确认收到", "status": "received",
                  "device_received": True, "at": "2026-11-07T16:01:00+08:00"})
    assert store.read(first)["status"] == "completed"
    assert store.read(second)["status"] == "running"


def test_restart_classifies_incomplete_send_without_replaying(tmp_path):
    store = TaskStateStore(tmp_path)
    run_id = "weixin-33333333"
    store.begin(run_id, goal="发消息", source="weixin", session_id="user")
    store.record({"id": run_id, "stage": "微信接口发送", "status": "running"})
    recovered = store.recover_interrupted()
    assert len(recovered) == 1
    assert recovered[0]["status"] == "awaiting_verification"
    assert recovered[0]["recovery_plan"] == "verify_before_retry"
    assert store.recover_interrupted() == []


def test_followup_links_to_selected_task_without_merging_goals(tmp_path):
    store = TaskStateStore(tmp_path)
    store.begin("weixin-parent111", goal="查找正面照片", source="weixin", session_id="same-user")
    store.begin("weixin-child2222", goal="发送第2个", source="weixin", session_id="same-user")
    child = store.link_parent("weixin-child2222", "weixin-parent111")
    assert child["parent_run_id"] == "weixin-parent111"
    assert child["goal"] == "发送第2个"
    assert store.read("weixin-parent111")["goal"] == "查找正面照片"


def test_long_plan_preserves_evidence_and_next_step(tmp_path):
    store = TaskStateStore(tmp_path)
    run_id = "long-task-123456"
    store.begin(run_id, goal="整理文件并通知", source="web", session_id="s1")
    store.set_plan(run_id, [{"title": "找文件", "expected": "确定唯一路径"},
                            {"title": "核对内容"}, {"title": "发送通知"}])
    state = store.complete_step(run_id, "1", "file:sha256-abc")
    assert state["next_step"] == "2"
    assert state["plan"][0]["evidence"] == "file:sha256-abc"
    assert store.complete_step(run_id, "1", "file:sha256-abc")["next_step"] == "2"
    try:
        store.complete_step(run_id, "3", "message:sent")
    except ValueError:
        pass
    else:
        assert False, "out-of-order side effect must be rejected"
    assert TaskStateStore(tmp_path).read(run_id)["next_step"] == "2"


def test_successful_intermediate_tool_does_not_complete_whole_task(tmp_path):
    store = TaskStateStore(tmp_path)
    store.begin("intermediate123", goal="先找文件再发送", source="weixin", session_id="s1")
    state = store.record({"id": "intermediate123", "stage": "工具执行", "tool": "search_files",
                          "status": "completed"})
    assert state["status"] == "running"


def test_send_step_keeps_expected_actual_and_safe_next_action(tmp_path):
    store = TaskStateStore(tmp_path)
    store.begin("evidence123", goal="发送天气提醒", source="weixin", session_id="s1")
    state = store.record({"id": "evidence123", "stage": "微信接口发送",
                          "status": "unconfirmed", "artifact_id": "hash123"})
    step = state["steps"][-1]
    assert step["expected"] == "微信发送接口明确接受请求"
    assert step["actual"] == "接口未返回明确状态"
    assert step["evidence_ref"] == "hash123"
    assert "先核对接收端" in step["next_action"]
    assert state["status"] == "awaiting_verification"
