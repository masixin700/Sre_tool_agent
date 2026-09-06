# -*- coding: utf-8 -*-
"""SRE Tool-Calling Agent（DeepSeek function calling + 三阶段状态机）。

用法：
  export DEEPSEEK_API_KEY=sk-xxxx          # 或把 key 写入工作目录 .deepseek_key
  python3 agent.py "access-bj-02 疑似环路，终端大面积掉线"
  python3 agent.py                          # 交互模式
  AGENT_AUTO_CONFIRM=1 python3 agent.py ... # 跳过二次确认（仅演示/测试）

状态机：
  阶段一 DETECT  发现异常 → 强制感知 + search_knowledge_base，禁止直接回答
  阶段二 DIAGNOSE 基于历史预案/变更关联给出诊断与处置建议
  阶段三 ACT     处置工具 → 二次确认 → 执行 → verify 复验
                 ├─ 恢复：close_incident_loop → "闭环完成"
                 └─ 未恢复：自动 rollback_last_action → 回到阶段二（限次后转人工）
"""
import json
import logging
import sys
import urllib.request
import urllib.error

import config
import tools
import notifier
import memory
from netbox_client import nb

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("agent")

SYSTEM_PROMPT = """你是一名 SRE 网络运维智能体，数据源与处置对象是 NetBox 网管系统（设备台账、异常工单 journal、接口状态）。
你必须通过工具调用来感知、分析、处置，严禁凭空编造设备状态或处置结果。

【三层工具栈】
- 感知层（只读，不改变状态）：list_active_incidents（未闭环异常清单，含 category 与 required_remediation）、
  get_device_detail、get_device_interfaces、verify_incident_resolved（闭环复验）。
- 分析层：search_knowledge_base（检索内部运维文档/历史故障预案，返回历史案例与 recommended_action_tool；
  默认 source=auto 同时检索本地预案库 local、内部文档系统 API（api）与经验记忆（experience，本 Agent 历史处置自动沉淀）；
  experience 条目中的 failed_attempts 是已被复验证伪并回滚的动作，严禁重复，其 recommended_action_tool 是已验证有效动作）、
  correlate_events（把异常时间戳附近的升级/回退/割接/发布等变更事件与异常关联比对）。
- 通知层（对外推送，不改变 NetBox 状态，无需二次确认）：send_notification（通过钉钉/飞书/企业微信群机器人推送）。
  以下场景应主动调用：①阶段一发现 danger 级业务受损异常，需立即通知值班人员；②需要人工确认或人工介入
  （如固件 Bootloader 恢复需现场 Console、自动处置多次失败、高危操作需值班在群里确认）；③用户明确要求发群通知。
  注意：闭环通报与转人工升级系统会自动推送（若已配置机器人），你不要再重复发送这两类消息；
  你的主动通知主要用于阶段一发现业务受损异常时的告警，以及需要人工确认但系统不会自动触发的场景。
- 处置层（高危写操作，必须用户二次确认；执行后系统自动复验，未恢复会自动回滚）：
  shutdown_loop_port（关闭环路端口）、deploy_loop_protection（环路防护）、adjust_buffer_queue（缓冲/队列）、
  troubleshoot_process_restart（排查进程/重启）、fix_oob_management（带外管理网络）、fix_ntp_source（NTP 源）、
  tune_soft_config（hello 计时器/CPU 阈值软配置）、fix_device_config（修正 VLAN/DHCP Snooping/BGP/ACL 等配置）、
  rollback_config（回滚错误变更/恢复误删路由）、rollback_last_action（回滚最近处置）。

【三阶段状态机 · 铁律】
阶段一 发现异常：不允许直接下结论。必须先用感知层工具核实异常，并强制调用 search_knowledge_base 检索历史预案。
阶段二 诊断建议：结合预案库历史案例与 correlate_events 关联结果，说明：异常现象、目标设备 id、故障类别、
  拟执行的处置工具及理由（处置工具必须与 category / recommended_action_tool 匹配），向用户说清处置方案。
阶段三 处置动作：诊断完成后直接调用处置工具即可，不要在自然语言中停下来等待用户确认
  ——二次确认由系统闸门统一处理（高危工具调用时系统会自动弹出 yes/no 确认，被取消也会把结果回传给你）。
  执行后系统自动复验：
  - 若恢复，系统完成闭环并告知你"闭环完成"，你再向用户总结；
  - 若未恢复，系统自动回滚该动作并把回滚结果回传给你——你必须重新回到阶段二分析
    （常见原因：处置工具与异常类别不匹配），严禁不加分析地重复同一动作。
每次处置只针对一台设备；需要关端口时先用 get_device_interfaces 拿到 interface_id。
"""

ANOMALY_HINTS = ["故障", "异常", "告警", "掉线", "不通", "丢包", "震荡", "漂移", "中断",
                 "环路", "cpu", "ntp", "时钟", "带外", "ospf", "排查", "处置", "恢复",
                 "无法", "失败", "卡顿", "泄漏", "误"]


# ---------------------------------------------------------------- DeepSeek API
def deepseek_chat(messages, with_tools=True):
    body = {
        "model": config.DEEPSEEK_MODEL,
        "messages": messages,
        "temperature": 0.2,
    }
    if with_tools:
        body["tools"] = tools.TOOL_SCHEMAS
        body["tool_choice"] = "auto"
    req = urllib.request.Request(
        config.DEEPSEEK_BASE_URL.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + config.DEEPSEEK_API_KEY,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError("DeepSeek API 错误 %s: %s" % (e.code, detail[:600]))


# ---------------------------------------------------------------- 交互辅助
def confirm_action(name, args):
    """处置层动作二次确认。"""
    layer = tools.TOOL_LAYER[name]
    if layer != "action" or name in tools.AUTO_SAFE_TOOLS:
        return True
    print("\n" + "=" * 68)
    print("⚠️  高危处置动作，需要二次确认")
    print("    工具：%s" % name)
    print("    参数：%s" % json.dumps(args, ensure_ascii=False))
    print("=" * 68)
    if config.AUTO_CONFIRM:
        print("    （AGENT_AUTO_CONFIRM=1，自动确认）")
        return True
    try:
        ans = input("    确认执行请输入 yes，取消请输入 no > ").strip().lower()
    except EOFError:
        return False
    return ans in ("yes", "y", "是")


def tool_result(obj):
    return json.dumps(obj, ensure_ascii=False)


# ---------------------------------------------------------------- 经验沉淀
def _distill_experience(state, device_id, device_name, incidents, result_status,
                        action, failed_attempts):
    """用确定性数据沉淀一条处置经验（不经 LLM 抽取，避免幻觉）。"""
    inc = (incidents or [{}])[0]
    entry = {
        "session_id": state.get("session_id"),
        "device": device_name,
        "device_id": device_id,
        "category": inc.get("category", "unknown"),
        "incident_summary": inc.get("summary", ""),
        "root_cause_hint": state.get("root_cause_hint", ""),
        "kb_refs": state.get("kb_refs", []),
        "result": result_status,                       # closed / escalated
        "attempts": len(failed_attempts) + (1 if result_status == "closed" else 0),
        "failed_attempts": failed_attempts,
    }
    if action:
        entry["effective_action"] = action
    rec = memory.record_experience(entry)
    if rec:
        memory.log_event(state.get("session_id"), "experience_recorded",
                         experience_id=rec["id"], device=device_name,
                         category=entry["category"], result=result_status)
    return rec


# ---------------------------------------------------------------- 处置 + 闭环
def execute_action_with_verification(name, args, state):
    """阶段三：执行处置动作 → 复验 → 闭环 or 自动回滚。"""
    device_id = args.get("device_id")
    result = tools.call_tool(name, args)
    if isinstance(result, dict) and result.get("error"):
        return result

    # 系统自动复验（感知层）
    verify = None
    if device_id is not None:
        verify = tools.verify_incident_resolved(device_id)
        result["verification"] = verify

    if verify and verify.get("all_resolved"):
        close = tools.close_incident_loop(device_id, verify)
        result["closed_loop"] = close
        state["closed_devices"].add(str(device_id))
        result["outcome"] = "闭环完成：异常已恢复，设备状态已更新，工单已标记闭环。"
        log.info("✅ 闭环完成：device=%s, closed_incidents=%s",
                 verify["device"], close["closed_incidents"])
        # 沉淀成功经验（含此前失败尝试，供后续避坑）
        _distill_experience(state, device_id, verify["device"],
                            verify.get("resolved_incidents"), "closed",
                            {"tool": name, "remediation_type": result.get("remediation_type"),
                             "journal_entry_id": result.get("journal_entry_id")},
                            state["failed_attempts"].get(str(device_id), []))
        memory.log_event(state.get("session_id"), "closed",
                         device=verify["device"], incidents=close["closed_incidents"])
        if "closed" in config.NOTIFY_EVENTS:
            notifier.notify(
                "【闭环通报】%s 异常处置完成" % verify["device"],
                "设备：%s\n处置动作：%s\n已闭环工单：%s\n设备状态：active，闭环完成。"
                % (verify["device"], name, close["closed_incidents"]),
                level="success")
    elif verify is not None:
        # 未恢复 → 自动触发回滚工具
        unresolved = "; ".join(
            "%s(category=%s)" % (u["summary"][:40], u["category"])
            for u in verify.get("unresolved_incidents", []))
        rb = tools.rollback_last_action(
            reason="处置后复验未恢复，未闭环异常：%s" % unresolved)
        result["auto_rollback"] = rb
        attempts = state["action_attempts"].get(str(device_id), 0) + 1
        state["action_attempts"][str(device_id)] = attempts
        # 记录本设备失败尝试（用于经验沉淀与避坑）
        failed_list = state["failed_attempts"].setdefault(str(device_id), [])
        failed_list.append({"tool": name,
                            "detail": "第%d次尝试复验未通过已自动回滚；未闭环：%s"
                                      % (attempts, unresolved[:120])})
        if attempts >= config.MAX_ACTION_ATTEMPTS:
            result["outcome"] = ("❌ 复验未通过且已达最大处置尝试次数（%d），动作已自动回滚，"
                                 "请转人工介入。" % config.MAX_ACTION_ATTEMPTS)
            # 沉淀失败经验（标记哪些动作已证伪，需人工/现场介入）
            _distill_experience(state, device_id, verify["device"],
                                verify.get("unresolved_incidents"), "escalated",
                                None, failed_list)
            memory.log_event(state.get("session_id"), "escalated",
                             device=verify["device"], attempts=attempts,
                             failed_tools=[f["tool"] for f in failed_list])
            # 自动升级：推送 IM 机器人请求人工确认/介入
            if "escalation" in config.NOTIFY_EVENTS:
                notifier.notify(
                    "【人工介入请求】%s 自动处置失败，需值班确认" % verify["device"],
                    "设备：%s（device_id=%s）\n失败动作：%s（已自动回滚）\n"
                    "未闭环异常：%s\n已尝试 %d 次仍未恢复。\n"
                    "请值班人员立即确认：现场排查 / 接管处置 / 调整方案。"
                    % (verify["device"], device_id, name, unresolved, attempts),
                    level="critical")
        else:
            result["outcome"] = (
                "❌ 处置后复验未通过，系统已自动回滚该动作（第 %d 次尝试）。"
                "请重新进入阶段二：用 search_knowledge_base / correlate_events 重新分析根因，"
                "选择与异常 category 真正匹配的处置工具，不要重复刚才的动作。" % attempts)
        log.warning("复验未通过，已自动回滚：%s", rb.get("message"))
    return result


# ---------------------------------------------------------------- 主循环
STAGE_DETECT, STAGE_DIAGNOSE, STAGE_ACT, STAGE_DONE = "DETECT", "DIAGNOSE", "ACT", "DONE"


def run(user_input):
    if not config.DEEPSEEK_API_KEY:
        print("❌ 未配置 DeepSeek API Key：请 export DEEPSEEK_API_KEY=sk-xxx "
              "或写入 .deepseek_key 文件")
        return

    print("🔌 连接 NetBox %s ..." % config.NETBOX_URL)
    nb.login()

    session_id = memory.start_session(user_input)   # 情景记忆：开启会话轨迹
    state = {"stage": STAGE_DETECT, "kb_searched": False,
             "action_attempts": {}, "closed_devices": set(),
             "session_id": session_id, "kb_refs": [], "root_cause_hint": "",
             "failed_attempts": {}}
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_input},
    ]

    max_rounds, guard_hits = 12, 0
    for round_i in range(1, max_rounds + 1):
        resp = deepseek_chat(messages)
        msg = resp["choices"][0]["message"]
        tool_calls = msg.get("tool_calls") or []

        if tool_calls:
            messages.append({
                "role": "assistant",
                "content": msg.get("content") or "",
                "tool_calls": tool_calls,
            })
            for tc in tool_calls:
                fn = tc["function"]
                name, raw_args = fn["name"], fn.get("arguments") or "{}"
                try:
                    args = json.loads(raw_args)
                except json.JSONDecodeError:
                    args = {}
                layer = tools.TOOL_LAYER.get(name, "?")
                layer_cn = {"perception": "感知层", "analysis": "分析层",
                            "action": "处置层", "notify": "通知层"}.get(layer, layer)
                print("\n🔧 工具调用 [%s·%s] %s(%s)"
                      % (layer_cn, layer, name,
                         json.dumps(args, ensure_ascii=False)))

                # ---- 状态机推进 ----
                if name == "search_knowledge_base":
                    state["kb_searched"] = True
                    if state["stage"] == STAGE_DETECT:
                        state["stage"] = STAGE_DIAGNOSE
                if layer == "action":
                    state["stage"] = STAGE_ACT

                # ---- 处置层：二次确认 ----
                if layer == "action" and not confirm_action(name, args):
                    result = {"cancelled": True,
                              "message": "用户在二次确认环节取消了该高危操作，处置未执行。"
                                         "请向用户说明可选的替代方案或等待进一步指示。"}
                    print("    ✖ 用户取消执行")
                elif layer == "action" and name != "rollback_last_action":
                    result = execute_action_with_verification(name, args, state)
                    if result.get("outcome"):
                        print("    → %s" % result["outcome"].split("。")[0])
                else:
                    result = tools.call_tool(name, args)

                # ---- 情景记忆：记录工具调用轨迹 + 抽取经验要素 ----
                memory.log_tool_call(session_id, name, layer, args, result)
                if name == "search_knowledge_base" and isinstance(result, dict):
                    for c in result.get("cases", []):
                        doc_id = c.get("doc_id")
                        if doc_id and c.get("source") == "local" and doc_id not in state["kb_refs"]:
                            state["kb_refs"].append(doc_id)
                if name == "correlate_events" and isinstance(result, dict):
                    state["root_cause_hint"] = result.get("conclusion", "")

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": tool_result(result),
                })
            continue

        # ---- 无工具调用：最终回答 ----
        content = (msg.get("content") or "").strip()
        used_tools = any(m.get("role") == "tool" for m in messages)
        is_anomaly = any(h in user_input.lower() for h in ANOMALY_HINTS)
        if (not used_tools and is_anomaly and not state["kb_searched"]
                and guard_hits < 2):
            guard_hits += 1
            print("⟦ 状态机守卫：阶段一未检索知识库，驳回直接回答 ⟧")
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content":
                "【状态机纪律提醒】阶段一发现异常不允许直接回答。请先调用感知层工具"
                "（list_active_incidents / get_device_detail）核实异常，"
                "并强制调用 search_knowledge_base 检索历史故障预案后再给诊断。"})
            continue

        print("\n" + "─" * 68)
        print(content)
        print("─" * 68)
        return

    print("⚠️ 已达最大推理轮次，退出。")


def repl():
    print("=" * 68)
    print("  SRE Tool-Calling Agent（DeepSeek + NetBox）")
    print("  数据源：%s  账号：%s" % (config.NETBOX_URL, config.NETBOX_USER))
    print("  输入故障描述开始；输入 quit 退出")
    print("=" * 68)
    while True:
        try:
            q = input("\n👤 > ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if q.lower() in ("quit", "exit", "q"):
            break
        if q:
            run(q)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if "--auto-confirm" in sys.argv:
        config.AUTO_CONFIRM = True
    if args:
        run(" ".join(args))
    else:
        repl()
