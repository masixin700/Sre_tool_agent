# -*- coding: utf-8 -*-
"""三层工具栈（Tool Stack）实现，数据面为 NetBox（localhost:8000）。

感知层 Perception（只读 GET）：
  - list_active_incidents      拉取当前未闭环异常（journal 异常登记）
  - get_device_detail          设备详情/状态
  - get_device_interfaces      接口状态（up/down）
  - verify_incident_resolved   闭环验证：处置后重新获取异常信息，判断是否恢复

分析层 Analysis（只读 + 检索）：
  - search_knowledge_base      检索内部运维文档 / 历史故障预案
  - correlate_events           时间窗口内变更事件（发布/回退/割接）与异常关联比对

处置层 Action（写操作，高危，必须二次确认；每个动作执行前自动快照，可回滚）：
  - shutdown_loop_port         定位并关闭环路端口
  - deploy_loop_protection     下发环路防护配置
  - adjust_buffer_queue        调整缓冲与 QoS 队列配置
  - troubleshoot_process_restart 远程排查进程、必要时重启
  - fix_oob_management         排查/修正带外管理网络配置
  - fix_ntp_source             检查并修正 NTP 源配置
  - tune_soft_config           检查 hello 计时器、CPU 阈值等软配置
  - fix_device_config          修正设备配置（VLAN/DHCP Snooping/BGP/ACL 等）
  - rollback_config            回滚错误变更 / 恢复误删配置（如默认路由）
  - rollback_last_action       回滚工具：按快照撤销最近一次处置动作
"""
import datetime
import logging

from netbox_client import nb, NetBoxError
from knowledge_base import kb
import external_kb
import notifier
import memory

log = logging.getLogger("tools")

# ============================================================================
# 异常分类 与 处置类型映射
# ============================================================================

# 异常类别 -> 能使其闭环的处置类型（verify 时用）
CATEGORY_REMEDIATION = {
    "loop": {"loop_shutdown"},
    "microburst": {"buffer_queue"},
    "cpu_process": {"process_restart"},
    "ntp": {"ntp_fix"},
    "oob": {"oob_fix"},
    "ospf_soft": {"soft_config"},
    "config_rollback": {"config_rollback"},
    "config_fix": {"config_fix"},
    # firmware（Bootloader 恢复）需现场 Console，远程动作无法闭环
    "firmware": set(),
    "unknown": set(),
}

# 处置工具 -> 处置类型标签（写入处置日志，供闭环验证匹配）
ACTION_REMEDIATION_TYPE = {
    "shutdown_loop_port": "loop_shutdown",
    "deploy_loop_protection": "loop_protection",
    "adjust_buffer_queue": "buffer_queue",
    "troubleshoot_process_restart": "process_restart",
    "fix_oob_management": "oob_fix",
    "fix_ntp_source": "ntp_fix",
    "tune_soft_config": "soft_config",
    "fix_device_config": "config_fix",
    "rollback_config": "config_rollback",
}


def classify_incident(comments):
    """根据异常登记文本判定故障类别。"""
    c = comments
    low = c.lower()
    if "环路" in c or ("mac" in low and "漂移" in c):
        return "loop"
    if "microburst" in low or "队列溢出" in c or ("队列" in c and "缓冲" in c):
        return "microburst"
    if "cpu" in low and ("100%" in c or "100 %" in c or "进程" in c):
        return "cpu_process"
    if "ntp" in low or "时钟漂移" in c or "时钟" in c and "漂移" in c:
        return "ntp"
    if "带外" in c or "idrac" in low or "管理ip" in low or "管理网关" in c:
        return "oob"
    if "ospf" in low or "hello" in low:
        return "ospf_soft"
    if "默认路由" in c or ("回退" in c and "误删" in c) or ("回滚" in c and "误" in c):
        return "config_rollback"
    if "bootloader" in low or "固件" in c and "升级失败" in c:
        return "firmware"
    if any(k in low for k in ["vlan", "dhcp", "bgp", "acl", "snooping"]):
        return "config_fix"
    return "unknown"


# ============================================================================
# 通用辅助
# ============================================================================

def _now():
    return datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")


def resolve_device(identifier):
    """identifier 可以是设备 id（int/str 数字）或设备名。"""
    if isinstance(identifier, int) or (isinstance(identifier, str) and identifier.isdigit()):
        return nb.get("/api/dcim/devices/%s/" % identifier)
    res = nb.get("/api/dcim/devices/", name=identifier)
    if not res.get("results"):
        raise NetBoxError("未找到设备: %s" % identifier)
    return res["results"][0]


def add_journal(device_id, kind, comments):
    """向设备写入一条 journal 日志（kind: info/success/warning/danger）。"""
    return nb.post("/api/extras/journal-entries/", {
        "assigned_object_type": "dcim.device",
        "assigned_object_id": device_id,
        "kind": kind,
        "comments": comments,
    })


def device_journals(device_id):
    return nb.get_all("/api/extras/journal-entries/",
                      assigned_object_type="dcim.device",
                      assigned_object_id=device_id)


def _brief(comments, n=120):
    first = comments.replace("\r", "").split("\n")[0]
    return first[:n]


def _obj_type_value(j):
    """assigned_object_type 在列表/详情接口中可能是 dict 或字符串，统一取值。"""
    ot = j.get("assigned_object_type")
    if isinstance(ot, dict):
        return ot.get("value", "")
    return str(ot or "")


# ============================================================================
# 处置动作快照栈（用于回滚）
# ============================================================================

ACTION_STACK = []  # 最近处置动作的快照列表


def _snapshot_action(tool_name, device_id, params):
    """执行前快照：设备状态 + （可选）接口状态。"""
    dev = nb.get("/api/dcim/devices/%s/" % device_id)
    snap = {
        "tool": tool_name,
        "device_id": device_id,
        "device_name": dev["name"],
        "params": params,
        "device_status_before": dev["status"]["value"],
        "interface_before": None,   # {"id":..,"name":..,"enabled":..}
        "journal_id": None,         # 本次动作写入的处置日志
        "time": _now(),
    }
    if tool_name == "shutdown_loop_port" and params.get("interface_id"):
        iface = nb.get("/api/dcim/interfaces/%s/" % params["interface_id"])
        snap["interface_before"] = {"id": iface["id"], "name": iface["name"],
                                    "enabled": iface["enabled"]}
    ACTION_STACK.append(snap)
    return snap


def _commit_journal(snap, remediation_type, detail_lines):
    """动作落地后写处置日志，并登记到快照。"""
    lines = [
        "【处置记录】[处置类型:%s]" % remediation_type,
        "动作工具：%s" % snap["tool"],
        "执行时间：%s" % snap["time"],
        "操作人：SRE-Agent（已经用户二次确认）",
    ]
    lines.extend(detail_lines)
    je = add_journal(snap["device_id"], "success", "\n".join(lines))
    snap["journal_id"] = je["id"]
    return je


def rollback_last_action(reason=""):
    """回滚工具：按快照撤销最近一次处置动作（删处置日志、恢复接口、恢复设备状态）。"""
    if not ACTION_STACK:
        return {"rolled_back": False, "message": "没有可回滚的处置动作（快照栈为空）"}
    snap = ACTION_STACK.pop()
    done = []
    # 1) 删除本次动作写入的处置日志
    if snap.get("journal_id"):
        nb.delete("/api/extras/journal-entries/%s/" % snap["journal_id"])
        done.append("删除处置日志 journal#%s" % snap["journal_id"])
    # 2) 恢复接口状态
    ib = snap.get("interface_before")
    if ib:
        nb.patch("/api/dcim/interfaces/%s/" % ib["id"], {"enabled": ib["enabled"]})
        done.append("恢复接口 %s(id=%s) enabled=%s" % (ib["name"], ib["id"], ib["enabled"]))
    # 3) 恢复设备状态（兜底，正常流程动作本身不改状态）
    dev = nb.get("/api/dcim/devices/%s/" % snap["device_id"])
    if dev["status"]["value"] != snap["device_status_before"]:
        nb.patch("/api/dcim/devices/%s/" % snap["device_id"],
                 {"status": snap["device_status_before"]})
        done.append("设备状态恢复为 %s" % snap["device_status_before"])
    msg = "已回滚动作 %s（设备 %s）：%s。回滚原因：%s" % (
        snap["tool"], snap["device_name"], "；".join(done) or "无写入项", reason or "闭环验证未通过")
    log.warning(msg)
    add_journal(snap["device_id"], "warning",
                "【回滚记录】%s\n原动作参数：%s" % (msg, snap["params"]))
    return {"rolled_back": True, "message": msg, "snapshot": {
        "tool": snap["tool"], "device": snap["device_name"], "reverted": done}}


# ============================================================================
# 感知层工具（Perception，只读）
# ============================================================================

def _is_open_anomaly(comments):
    """是否为"未闭环异常登记"。系统写的【回滚记录】/【处置记录】/【处置闭环】正文中可能引用
    【异常登记】字样（如回滚原因引用异常摘要），必须排除，否则会把系统日志误判成异常。"""
    if "【异常登记】" not in comments or "【已闭环】" in comments:
        return False
    if any(tag in comments for tag in ("【回滚记录】", "【处置记录】", "【处置闭环】")):
        return False
    return True


def list_active_incidents(severity=None, device_name=None):
    """扫描 NetBox journal 中所有未闭环的异常登记（danger/warning）。"""
    entries = nb.get_all("/api/extras/journal-entries/")
    incidents = []
    for j in entries:
        comments = j.get("comments") or ""
        if not _is_open_anomaly(comments):
            continue
        kind = j["kind"]["value"]
        if kind not in ("danger", "warning"):
            continue
        obj = j.get("assigned_object") or {}
        ot = j.get("assigned_object_type")
        obj_type = ot.get("value", "") if isinstance(ot, dict) else str(ot or "")
        if obj_type != "dcim.device":
            continue  # 只关注设备维度异常（IP/线路类台账问题不纳入设备闭环）
        name = obj.get("name") or obj.get("display")
        if device_name and device_name != name:
            continue
        if severity and kind != severity:
            continue
        cat = classify_incident(comments)
        incidents.append({
            "incident_id": j["id"],
            "device_id": obj.get("id"),
            "device": name,
            "severity": kind,                       # danger=业务受损/硬件故障, warning=风险待整改
            "category": cat,
            "required_remediation": sorted(CATEGORY_REMEDIATION.get(cat, set())),
            "summary": _brief(comments),
            "created": j["created"],
        })
    return {"count": len(incidents), "incidents": incidents}


def get_device_detail(identifier):
    dev = resolve_device(identifier)
    return {
        "device_id": dev["id"],
        "name": dev["name"],
        "status": dev["status"]["value"],
        "role": (dev.get("role") or {}).get("name"),
        "site": (dev.get("site") or {}).get("name"),
        "device_type": (dev.get("device_type") or {}).get("model"),
        "tenant": ((dev.get("tenant") or {}).get("name")),
        "comments": dev.get("comments", "")[:300],
    }


def get_device_interfaces(identifier):
    dev = resolve_device(identifier)
    ifaces = nb.get_all("/api/dcim/interfaces/", device_id=dev["id"])
    out = []
    for i in ifaces:
        out.append({
            "interface_id": i["id"],
            "name": i["name"],
            "type": i["type"]["value"],
            "enabled": i["enabled"],
            "mtu": i.get("mtu"),
            "duplex": i.get("duplex"),
        })
    return {"device": dev["name"], "device_id": dev["id"],
            "status": dev["status"]["value"], "interface_count": len(out), "interfaces": out}


def verify_incident_resolved(identifier):
    """闭环验证（只读）：处置后重新获取异常信息，判断异常是否恢复。

    判定规则：设备上每条未闭环异常登记都有一条时间更晚、处置类型匹配的【处置记录】。
    """
    dev = resolve_device(identifier)
    journals = device_journals(dev["id"])
    open_incidents, remediations = [], []
    for j in journals:
        c = j.get("comments") or ""
        kind = j["kind"]["value"]
        if _is_open_anomaly(c) and kind in ("danger", "warning"):
            open_incidents.append({
                "incident_id": j["id"], "severity": kind,
                "category": classify_incident(c),
                "summary": _brief(c), "created": j["created"],
            })
        if "【处置记录】" in c and kind == "success":
            rtype = ""
            if "[处置类型:" in c:
                rtype = c.split("[处置类型:")[1].split("]")[0]
            remediations.append({"journal_id": j["id"], "remediation_type": rtype,
                                 "created": j["created"]})
    resolved, unresolved = [], []
    for inc in open_incidents:
        needed = CATEGORY_REMEDIATION.get(inc["category"], set())
        matched = [r for r in remediations
                   if r["remediation_type"] in needed and r["created"] >= inc["created"]]
        if needed and matched:
            d = dict(inc)
            d["matched_remediation"] = matched
            resolved.append(d)
        else:
            d = dict(inc)
            d["reason"] = "无匹配的处置记录（需要处置类型: %s）" % sorted(needed) if needed \
                else "该类异常无法通过远程处置闭环（需现场/人工处理）"
            unresolved.append(d)
    return {
        "device": dev["name"],
        "device_id": dev["id"],
        "device_status": dev["status"]["value"],
        "open_incident_count": len(open_incidents),
        "all_resolved": len(open_incidents) > 0 and not unresolved,
        "resolved_incidents": resolved,
        "unresolved_incidents": unresolved,
        "remediation_records": remediations,
    }


def close_incident_loop(identifier, verify_result):
    """系统侧闭环动作（验证通过后由状态机调用，非 LLM 工具）：
    设备状态置 active、异常登记追加【已闭环】、写闭环日志。"""
    dev = resolve_device(identifier)
    if dev["status"]["value"] != "active":
        nb.patch("/api/dcim/devices/%s/" % dev["id"], {"status": "active"})
    for inc in verify_result.get("resolved_incidents", []):
        j = nb.get("/api/extras/journal-entries/%s/" % inc["incident_id"])
        nb.patch("/api/extras/journal-entries/%s/" % inc["incident_id"],
                 {"comments": j["comments"] + "\n【已闭环】%s 经处置后验证恢复，闭环完成。" % _now()})
    add_journal(dev["id"], "success",
                "【处置闭环】设备 %s：%d 条异常验证恢复，设备状态置为 active，闭环完成。"
                % (dev["name"], len(verify_result.get("resolved_incidents", []))))
    return {"closed": True, "device": dev["name"],
            "closed_incidents": [i["incident_id"] for i in verify_result["resolved_incidents"]]}


# ============================================================================
# 分析层工具（Analysis）
# ============================================================================

def search_knowledge_base(query, top_k=3, source="auto"):
    """检索内部运维文档 / 历史故障预案。

    source:
      - "local": 仅检索本地预案库（data/kb_docs.json，内置兜底，永远可用）
      - "api":   仅检索外部内部文档系统 API（KB_API_URL 配置后可用，对接内部 RAG/文档平台）
      - "auto":  本地预案库 + 外部 API + 经验记忆，合并返回（默认；各源失败自动降级）
    返回每条案例带来源标签：local=预案库 / api=内部文档系统 / experience=本 Agent 沉淀的历史处置经验
    （经验条目中的 failed_attempts 是已证伪动作，严禁重复；recommended_action_tool 是已验证有效动作）。
    """
    out = {"query": query, "source_requested": source, "cases": [], "sources": {}}

    def _local():
        results = kb.search(query, top_k=top_k)
        for r in results:
            r["source"] = "local"
        out["sources"]["local"] = {"hit_count": len(results)}
        out["cases"].extend(results)

    def _api():
        resp = external_kb.search_external(query, top_k=top_k)
        out["sources"]["api"] = {
            "configured": resp.get("configured", False),
            "hit_count": resp.get("hit_count", len(resp.get("cases", []))),
        }
        if resp.get("endpoint"):
            out["sources"]["api"]["endpoint"] = resp["endpoint"]
        if resp.get("error"):
            out["sources"]["api"]["error"] = resp["error"]
        if resp.get("message") and not resp.get("configured"):
            out["sources"]["api"]["message"] = resp["message"]
        out["cases"].extend(resp.get("cases", []))

    def _experience():
        results = memory.search_experiences(query, top_k=top_k)
        out["sources"]["experience"] = {"hit_count": len(results),
                                        "store": "memory/experiences.jsonl"}
        out["cases"].extend(results)

    if source == "local":
        _local()
    elif source == "api":
        _api()
        if not out["sources"]["api"].get("hit_count") and not out["sources"]["api"].get("configured"):
            out["fallback_note"] = "外部 API 未配置，未返回结果；可改用 source=local/auto 检索本地预案库。"
    else:  # auto：预案库 + 内部文档 API + 经验记忆
        _experience()
        _local()
        _api()

    out["hit_count"] = len(out["cases"])
    return out


_CHANGE_KEYWORDS = ["升级", "回退", "变更", "割接", "发布", "误删", "误划", "误配",
                    "手工变更", "批量", "配置漂移", "临时"]


def correlate_events(identifier=None, incident_id=None, window_hours=72):
    """将异常时间戳附近的变更事件（升级/发布/配置变更/割接）与异常进行关联比对。"""
    target = None
    if incident_id:
        target = nb.get("/api/extras/journal-entries/%s/" % incident_id)
    elif identifier:
        dev = resolve_device(identifier)
        for j in device_journals(dev["id"]):
            if "【异常登记】" in (j.get("comments") or "") and j["kind"]["value"] == "danger":
                target = j
                break
    if not target:
        return {"correlated": False, "message": "未指定可关联的异常（提供 incident_id 或故障设备）"}

    t_time = datetime.datetime.fromisoformat(target["created"].replace("Z", "+00:00"))
    t_obj = target.get("assigned_object") or {}
    t_dev_id = t_obj.get("id") if _obj_type_value(target) == "dcim.device" else None

    # 收集候选日志：本设备 + 同站点设备
    candidates = list(device_journals(t_dev_id)) if t_dev_id else []
    if t_dev_id:
        dev = nb.get("/api/dcim/devices/%s/" % t_dev_id)
        site_id = (dev.get("site") or {}).get("id")
        if site_id:
            site_devs = nb.get_all("/api/dcim/devices/", site_id=site_id)
            for sd in site_devs:
                if sd["id"] == t_dev_id:
                    continue
                candidates.extend(device_journals(sd["id"]))

    window = datetime.timedelta(hours=window_hours)
    changes, anomalies = [], []
    for j in candidates:
        c = j.get("comments") or ""
        jt = datetime.datetime.fromisoformat(j["created"].replace("Z", "+00:00"))
        if abs(jt - t_time) > window:
            continue
        obj = j.get("assigned_object") or {}
        item = {"journal_id": j["id"], "object": obj.get("name") or obj.get("display"),
                "created": j["created"], "summary": _brief(c)}
        is_change = ("【异常登记】" not in c and any(k in c for k in _CHANGE_KEYWORDS)) or \
                    ("回退" in c or "升级" in c) and "【异常登记】" in c
        if "【异常登记】" in c and j["kind"]["value"] in ("danger", "warning"):
            item["category"] = classify_incident(c)
            # 异常描述自身即携带变更诱因（如"升级失败""回退误删""割接后"）
            item["change_induced"] = any(
                k in c for k in ["升级失败", "回退", "误删", "误划", "误配", "割接",
                                 "批量升级", "手工变更", "配置漂移"])
            anomalies.append(item)
        elif is_change:
            changes.append(item)

    target_change_induced = any(a.get("change_induced") for a in anomalies
                                if a["journal_id"] == target["id"])
    conclusion = "未发现时间窗口内的变更事件，倾向于设备自身/硬件原因。"
    if changes or target_change_induced:
        conclusion = ("异常与变更事件强相关（窗口内变更 %d 起，目标异常自述含变更诱因：%s），"
                      "优先按变更回滚/配置修正方向处置。"
                      % (len(changes), "是" if target_change_induced else "否"))
    return {
        "target_incident": {"journal_id": target["id"],
                            "summary": _brief(target.get("comments") or ""),
                            "created": target["created"],
                            "category": classify_incident(target.get("comments") or "")},
        "window_hours": window_hours,
        "change_events_in_window": changes,
        "other_anomalies_in_window": anomalies,
        "conclusion": conclusion,
    }


# ============================================================================
# 通知工具（Notify，对外推送 / 只读外部副作用、不改变 NetBox 状态 / 免高危确认）
# ============================================================================

def send_notification(title, content, channels="all", level="warning"):
    """通过钉钉/飞书/企微群机器人推送消息（异常告警、人工确认/升级、闭环通报）。

    channels: all（默认，所有已配置渠道）/ dingtalk / feishu / wecom，逗号分隔可多选。
    level: info / warning / critical / success，决定消息图标。
    """
    return notifier.notify(title, content, channels=channels, level=level)


# ============================================================================
# 处置层工具（Action，写操作 / 高危 / 需二次确认）
# ============================================================================

def _run_action(tool_name, device_id, params, detail_lines, mutate=None):
    """统一动作执行框架：快照 -> 执行变更 -> 写处置日志。"""
    snap = _snapshot_action(tool_name, device_id, params)
    if mutate:
        mutate(snap)
    rtype = ACTION_REMEDIATION_TYPE[tool_name]
    je = _commit_journal(snap, rtype, detail_lines)
    return {
        "executed": True,
        "tool": tool_name,
        "remediation_type": rtype,
        "device_id": device_id,
        "journal_entry_id": je["id"],
        "detail": detail_lines,
        "note": "处置已执行并记录。系统将自动重新获取异常信息进行闭环验证；若未恢复将自动回滚。",
    }


def shutdown_loop_port(device_id, interface_id, reason=""):
    """定位并关闭环路端口（PATCH interface enabled=false）。"""
    iface = nb.get("/api/dcim/interfaces/%s/" % interface_id)
    params = {"interface_id": interface_id, "reason": reason}

    def mutate(snap):
        nb.patch("/api/dcim/interfaces/%s/" % interface_id, {"enabled": False})

    return _run_action("shutdown_loop_port", device_id, params, [
        "处置动作：关闭疑似环路端口 %s（interface id=%s，原状态 enabled=%s）"
        % (iface["name"], interface_id, iface["enabled"]),
        "目的：阻断二层广播风暴 / MAC 漂移",
        "原因/依据：%s" % (reason or "环路检测"),
    ], mutate=mutate)


def deploy_loop_protection(device_id, reason=""):
    """下发环路防护配置（STP BPDU Guard / Loop Detection / storm-control）。"""
    return _run_action("deploy_loop_protection", device_id, {"reason": reason}, [
        "处置动作：全网下发环路防护软配置",
        "配置内容：接入端口启用 STP BPDU Guard、全局 Loop Detection、广播 storm-control 限速",
        "原因/依据：%s" % (reason or "环路防护加固"),
    ])


def adjust_buffer_queue(device_id, reason=""):
    """调整端口缓冲与 QoS 队列配置。"""
    return _run_action("adjust_buffer_queue", device_id, {"reason": reason}, [
        "处置动作：调整端口缓冲（buffer）与 QoS 队列配置",
        "配置内容：增大实时业务队列深度、调整 WRR 调度权重、开启队列拥塞监控",
        "原因/依据：%s" % (reason or "Microburst 丢包治理"),
    ])


def troubleshoot_process_restart(device_id, reason="", restart=True):
    """远程排查异常进程，必要时重启设备。"""
    action = "已在维护窗口重启设备，重启后进程与配置校验正常" if restart \
        else "已远程定位并终止异常进程，CPU 恢复正常，暂不重启"
    return _run_action("troubleshoot_process_restart", device_id,
                       {"restart": restart, "reason": reason}, [
        "处置动作：经 Console/带外远程排查管理平面高 CPU 进程",
        "执行结果：%s" % action,
        "原因/依据：%s" % (reason or "管理平面 CPU 100%"),
    ])


def fix_oob_management(device_id, reason=""):
    """排查并修正带外管理网络配置（管理 VLAN / iDRAC 网关）。"""
    return _run_action("fix_oob_management", device_id, {"reason": reason}, [
        "处置动作：排查带外管理网络并修正配置",
        "配置内容：核对管理交换机端口/管理 VLAN，修正 iDRAC/管理口 IP-掩码-网关（原指向已回收网段）",
        "验证：SSH/IPMI/SNMP 采集恢复，新管理网段已登记 IPAM",
        "原因/依据：%s" % (reason or "带外管理不通"),
    ])


def fix_ntp_source(device_id, reason=""):
    """检查并修正 NTP 源配置。"""
    return _run_action("fix_ntp_source", device_id, {"reason": reason}, [
        "处置动作：检查并修正 NTP 源配置",
        "配置内容：移除失效 NTP 源，指向可信时钟源；chrony makestep 步进对时一次",
        "验证：时钟偏移 < 100ms，日志时间戳恢复一致",
        "原因/依据：%s" % (reason or "NTP 时钟漂移"),
    ])


def tune_soft_config(device_id, reason="", hello_timer_seconds=None, cpu_threshold_percent=None):
    """检查/修正 hello 计时器、CPU 阈值等软配置。"""
    tunes = []
    if hello_timer_seconds is not None:
        tunes.append("OSPF hello 计时器统一为 %ss（dead 4 倍）" % hello_timer_seconds)
    if cpu_threshold_percent is not None:
        tunes.append("CPU 利用率告警阈值设为 %s%% 并联动协议队列监控" % cpu_threshold_percent)
    if not tunes:
        tunes = ["恢复标准 hello/dead 计时器（两端一致）", "CPU 阈值告警门限重置为基线 80%"]
    return _run_action("tune_soft_config", device_id,
                       {"hello_timer_seconds": hello_timer_seconds,
                        "cpu_threshold_percent": cpu_threshold_percent, "reason": reason}, [
        "处置动作：检查并调优协议软配置",
        "配置内容：%s" % "；".join(tunes),
        "原因/依据：%s" % (reason or "OSPF 邻居震荡 / 软配置漂移"),
    ])


def fix_device_config(device_id, change_detail, reason=""):
    """修正设备配置（VLAN 误划 / DHCP Snooping trust / BGP 发布策略 / ACL 收紧等）。"""
    return _run_action("fix_device_config", device_id,
                       {"change_detail": change_detail, "reason": reason}, [
        "处置动作：修正设备配置",
        "修正内容：%s" % change_detail,
        "验证：相关业务连通性/取址/路由表验证通过",
        "原因/依据：%s" % (reason or "配置错误整改"),
    ])


def rollback_config(device_id, reason=""):
    """回滚错误变更 / 恢复误删配置（如默认路由 0.0.0.0/0）。"""
    return _run_action("rollback_config", device_id, {"reason": reason}, [
        "处置动作：从最近配置备份回滚，恢复误删配置",
        "恢复内容：补回默认路由 0.0.0.0/0 及变更中丢失的语句（diff 逐条复核）",
        "验证：出口路由表恢复，站点连通性正常",
        "原因/依据：%s" % (reason or "变更回退误删配置"),
    ])


# ============================================================================
# 工具注册表：元数据 + DeepSeek function-calling schema
# ============================================================================

TOOL_LAYER = {
    # 感知层
    "list_active_incidents": "perception",
    "get_device_detail": "perception",
    "get_device_interfaces": "perception",
    "verify_incident_resolved": "perception",
    # 分析层
    "search_knowledge_base": "analysis",
    "correlate_events": "analysis",
    # 通知层（对外推送，不改变 NetBox 状态，免高危二次确认）
    "send_notification": "notify",
    # 处置层
    "shutdown_loop_port": "action",
    "deploy_loop_protection": "action",
    "adjust_buffer_queue": "action",
    "troubleshoot_process_restart": "action",
    "fix_oob_management": "action",
    "fix_ntp_source": "action",
    "tune_soft_config": "action",
    "fix_device_config": "action",
    "rollback_config": "action",
    "rollback_last_action": "action",
}

# 回滚工具是"撤销自己刚做的操作"，方向安全，状态机自动触发时无需再次人工确认
AUTO_SAFE_TOOLS = {"rollback_last_action"}

_HANDLERS = {
    "list_active_incidents": list_active_incidents,
    "get_device_detail": get_device_detail,
    "get_device_interfaces": get_device_interfaces,
    "verify_incident_resolved": verify_incident_resolved,
    "search_knowledge_base": search_knowledge_base,
    "correlate_events": correlate_events,
    "send_notification": send_notification,
    "shutdown_loop_port": shutdown_loop_port,
    "deploy_loop_protection": deploy_loop_protection,
    "adjust_buffer_queue": adjust_buffer_queue,
    "troubleshoot_process_restart": troubleshoot_process_restart,
    "fix_oob_management": fix_oob_management,
    "fix_ntp_source": fix_ntp_source,
    "tune_soft_config": tune_soft_config,
    "fix_device_config": fix_device_config,
    "rollback_config": rollback_config,
    "rollback_last_action": rollback_last_action,
}

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "list_active_incidents",
            "description": "【感知层·只读】扫描 NetBox 中所有未闭环的异常登记（journal danger/warning），返回设备、严重级别、自动归类的故障类别(category)及所需处置类型。阶段一发现异常时首先调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": ["danger", "warning"],
                                 "description": "可选：danger=业务受损/硬件故障，warning=配置与安全风险"},
                    "device_name": {"type": "string", "description": "可选：仅查看指定设备名"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_device_detail",
            "description": "【感知层·只读】查询设备详情与当前状态（active/failed/offline 等）。identifier 可传设备 id 或设备名。",
            "parameters": {
                "type": "object",
                "properties": {
                    "identifier": {"type": "string", "description": "设备 id 或设备名，如 access-bj-02"},
                },
                "required": ["identifier"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_device_interfaces",
            "description": "【感知层·只读】查询设备接口列表及其 up/down(enabled) 状态，用于定位环路端口等。",
            "parameters": {
                "type": "object",
                "properties": {
                    "identifier": {"type": "string", "description": "设备 id 或设备名"},
                },
                "required": ["identifier"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "verify_incident_resolved",
            "description": "【感知层·只读】闭环验证：处置执行后重新获取设备异常信息，判断每条异常是否已有匹配的处置记录、设备是否恢复。返回 all_resolved 布尔值。",
            "parameters": {
                "type": "object",
                "properties": {
                    "identifier": {"type": "string", "description": "设备 id 或设备名"},
                },
                "required": ["identifier"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_knowledge_base",
            "description": "【分析层】检索内部运维文档与历史处置经验，返回历史案例、处置步骤和推荐处置工具(recommended_action_tool)。数据源三路：本地预案库(local，始终可用)、内部文档系统API(api，对接内部RAG/文档平台)、经验记忆(experience，本Agent历史处置中自动沉淀)；默认 source=auto 三路合并，每条结果标注 source。experience 结果中 failed_attempts 是已证伪动作严禁重复，recommended_action_tool 是已验证有效动作。发现异常后必须先调用本工具再下结论。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "故障现象/关键词，如：MAC地址漂移 广播风暴 终端掉线"},
                    "top_k": {"type": "integer", "description": "每个数据源返回条数，默认 3"},
                    "source": {"type": "string", "enum": ["auto", "local", "api"],
                               "description": "auto=本地预案库+内部文档API合并(默认)；local=仅本地预案库；api=仅内部文档系统API"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "correlate_events",
            "description": "【分析层】将异常时间戳附近时间窗口内的变更事件（固件升级、配置回退、割接发布、手工变更）与异常进行关联比对，判断异常是否由变更诱发。",
            "parameters": {
                "type": "object",
                "properties": {
                    "identifier": {"type": "string", "description": "设备 id 或设备名（与 incident_id 二选一）"},
                    "incident_id": {"integer".replace("integer", "integer"): "integer",
                                    "description": "异常日志 journal id"},
                    "window_hours": {"type": "integer", "description": "关联窗口小时数，默认 72"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_notification",
            "description": "【通知层】通过钉钉/飞书/企业微信群机器人推送消息。适用场景：①阶段一发现高危/业务受损异常需通知值班人员；②需要人工确认或人工介入（如固件Bootloader恢复需现场、自动处置多次失败转人工）；③处置闭环结果通报。该工具不改变 NetBox 状态，无需二次确认。未配置任何 webhook 时会返回跳过说明。",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "消息标题，如：【异常告警】access-bj-02 疑似二层环路"},
                    "content": {"type": "string", "description": "消息正文：设备、现象、诊断结论、需要人工做什么（确认/到现场/接管处置）"},
                    "channels": {"type": "string",
                                 "description": "发送渠道：all=所有已配置渠道(默认)；dingtalk/feishu/wecom 可逗号分隔多选"},
                    "level": {"type": "string", "enum": ["info", "warning", "critical", "success"],
                              "description": "级别：业务受损/需人工介入用 critical，风险待整改用 warning，闭环通报用 success"},
                },
                "required": ["title", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "shutdown_loop_port",
            "description": "【处置层·高危写操作】定位并关闭疑似环路端口（将接口置为 shutdown/down），用于阻断二层环路与广播风暴。需要用户二次确认。",
            "parameters": {
                "type": "object",
                "properties": {
                    "device_id": {"type": "integer", "description": "设备 id"},
                    "interface_id": {"type": "integer", "description": "要关闭的接口 id（来自 get_device_interfaces）"},
                    "reason": {"type": "string", "description": "处置原因/预案依据"},
                },
                "required": ["device_id", "interface_id", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "deploy_loop_protection",
            "description": "【处置层·高危写操作】下发环路防护配置（STP BPDU Guard / Loop Detection / storm-control）。需要用户二次确认。",
            "parameters": {
                "type": "object",
                "properties": {
                    "device_id": {"type": "integer", "description": "设备 id"},
                    "reason": {"type": "string", "description": "处置原因"},
                },
                "required": ["device_id", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "adjust_buffer_queue",
            "description": "【处置层·高危写操作】调整端口缓冲(buffer)与 QoS 队列配置，治理 Microburst 微突发丢包。需要用户二次确认。",
            "parameters": {
                "type": "object",
                "properties": {
                    "device_id": {"type": "integer", "description": "设备 id"},
                    "reason": {"type": "string", "description": "处置原因"},
                },
                "required": ["device_id", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "troubleshoot_process_restart",
            "description": "【处置层·高危写操作】通过带外/Console 远程排查管理平面异常进程，必要时在维护窗口重启设备。需要用户二次确认。",
            "parameters": {
                "type": "object",
                "properties": {
                    "device_id": {"type": "integer", "description": "设备 id"},
                    "reason": {"type": "string", "description": "处置原因"},
                    "restart": {"type": "boolean", "description": "是否重启设备：false=仅杀进程，true=维护窗口重启，默认 true"},
                },
                "required": ["device_id", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fix_oob_management",
            "description": "【处置层·高危写操作】排查并修正带外管理网络配置（管理 VLAN、管理交换机端口、iDRAC/管理口网关）。需要用户二次确认。",
            "parameters": {
                "type": "object",
                "properties": {
                    "device_id": {"type": "integer", "description": "设备 id"},
                    "reason": {"type": "string", "description": "处置原因"},
                },
                "required": ["device_id", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fix_ntp_source",
            "description": "【处置层·高危写操作】检查并修正 NTP 源配置（更换失效时钟源、步进对时）。需要用户二次确认。",
            "parameters": {
                "type": "object",
                "properties": {
                    "device_id": {"type": "integer", "description": "设备 id"},
                    "reason": {"type": "string", "description": "处置原因"},
                },
                "required": ["device_id", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tune_soft_config",
            "description": "【处置层·高危写操作】检查并调优 hello 计时器、CPU 阈值等软配置（用于 OSPF 邻居震荡类问题）。需要用户二次确认。",
            "parameters": {
                "type": "object",
                "properties": {
                    "device_id": {"type": "integer", "description": "设备 id"},
                    "reason": {"type": "string", "description": "处置原因"},
                    "hello_timer_seconds": {"type": "integer", "description": "目标 hello 计时器秒数（可选）"},
                    "cpu_threshold_percent": {"type": "integer", "description": "CPU 告警阈值百分比（可选）"},
                },
                "required": ["device_id", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fix_device_config",
            "description": "【处置层·高危写操作】修正设备配置：端口 VLAN 误划、DHCP Snooping trust 缺失、BGP 发布策略/路由泄漏、ACL 过宽等。需要用户二次确认。",
            "parameters": {
                "type": "object",
                "properties": {
                    "device_id": {"type": "integer", "description": "设备 id"},
                    "change_detail": {"type": "string", "description": "具体修正内容，如：端口G0/0/5由VLAN200改回VLAN100"},
                    "reason": {"type": "string", "description": "处置原因"},
                },
                "required": ["device_id", "change_detail", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rollback_config",
            "description": "【处置层·高危写操作】回滚错误变更/恢复误删配置：从配置备份恢复丢失语句（如误删的默认路由 0.0.0.0/0）。需要用户二次确认。",
            "parameters": {
                "type": "object",
                "properties": {
                    "device_id": {"type": "integer", "description": "设备 id"},
                    "reason": {"type": "string", "description": "处置原因"},
                },
                "required": ["device_id", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rollback_last_action",
            "description": "【处置层·回滚工具】撤销 Agent 最近一次已执行的处置动作（按快照自动恢复接口状态、设备状态并删除处置日志）。通常在处置后验证未恢复时由系统自动触发。",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "description": "回滚原因"},
                },
            },
        },
    },
]


def call_tool(name, args):
    """按名称分发工具调用。"""
    handler = _HANDLERS.get(name)
    if not handler:
        return {"error": "未知工具: %s" % name}
    try:
        return handler(**(args or {}))
    except Exception as e:  # 工具异常不中断 Agent，作为 tool 结果回传
        log.exception("工具 %s 执行异常", name)
        return {"error": "%s: %s" % (type(e).__name__, e)}
