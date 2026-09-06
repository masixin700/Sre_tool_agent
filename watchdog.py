# -*- coding: utf-8 -*-
"""定时巡逻器（Watchdog）—— Agent 的"自动发现异常"触发器。

模式：轮询（Pull）。平时 Agent 是睡着的，本进程按固定间隔调用感知层工具
list_active_incidents 扫描 NetBox 未闭环异常，与本地状态文件比对：

  - 新出现的异常 → 发 IM 告警（钉钉/飞书/企微，含设备/级别/类别/处置建议）；
    若 WATCH_AUTO_REMEDIATE=1，则自动调用 Agent 三阶段流程处置（自动确认，无 TTY）。
  - 已告警异常消失（被闭环）→ 发恢复通知（WATCH_NOTIFY_RECOVERY 控制）。
  - NetBox 连续扫描失败 → 达到阈值后告警一次，恢复后清零。

去重：以 journal 异常记录 ID（incident_id）为唯一键精确匹配，状态持久化到
memory/patrol_state.json，重启不丢、不重复告警。首巡默认只建基线不告警。

用法：
  python3 watchdog.py            # 常驻进程，每 WATCH_INTERVAL 秒巡逻一次
  python3 watchdog.py --once     # 单次巡逻（供 crontab / launchd 定时拉起）
"""
import json
import logging
import os
import sys
import time

import config
import tools
import notifier
from netbox_client import nb

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("watchdog")


# ---------------------------------------------------------------- 状态持久化
def load_state():
    try:
        with open(config.WATCH_STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"baseline_done": False, "incidents": {},
                "scan_fail_count": 0, "scan_fail_alerted": False}


def save_state(state):
    try:
        os.makedirs(os.path.dirname(config.WATCH_STATE_FILE), exist_ok=True)
        # 状态文件只保留最近 500 条，防止无限膨胀
        if len(state["incidents"]) > 500:
            keep = sorted(state["incidents"].items(),
                          key=lambda kv: kv[1].get("last_seen", ""))[-500:]
            state["incidents"] = dict(keep)
        tmp = config.WATCH_STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
        os.replace(tmp, config.WATCH_STATE_FILE)
    except Exception as e:
        log.warning("状态文件保存失败（不影响本次巡逻）: %s", e)


# ---------------------------------------------------------------- 告警内容
def _severity_zh(sev):
    return {"danger": "业务受损(danger)", "warning": "风险待整改(warning)"}.get(sev, sev)


def alert_new_incident(inc):
    title = "【巡逻告警·新异常】%s %s" % (inc["device"],
                                       "业务受损" if inc["severity"] == "danger" else "风险待整改")
    content = (
        "设备：%s（device_id=%s）\n"
        "级别：%s\n"
        "异常类别：%s\n"
        "现象：%s\n"
        "建议处置类型：%s\n"
        "处置方式：%s\n"
        "人工处理：python3 agent.py \"%s 异常：%s\""
        % (inc["device"], inc["device_id"], _severity_zh(inc["severity"]),
           inc["category"], inc["summary"][:150],
           "、".join(inc.get("required_remediation") or []) or "见知识库预案",
           "已开启自动处置，Agent 将自动执行三阶段流程" if config.WATCH_AUTO_REMEDIATE
           else "默认仅告警，请值班确认后启动 Agent 处置（或开启 WATCH_AUTO_REMEDIATE=1）",
           inc["device"], inc["summary"][:80]))
    level = "critical" if inc["severity"] == "danger" else "warning"
    notifier.notify(title, content, level=level)


def alert_recovery(inc, record):
    title = "【恢复通报】%s 异常已闭环" % inc["device"]
    content = (
        "设备：%s\n异常：%s（类别 %s，%s）\n"
        "该异常已%s，当前未闭环清单中已消失。"
        % (inc["device"], record.get("summary", "")[:120], record.get("category", "-"),
           _severity_zh(record.get("severity", "-")),
           "由巡逻 Agent 自动处置闭环" if inc.get("auto") else "闭环恢复（人工/其他途径）")
    )
    notifier.notify(title, content, level="success")


# ---------------------------------------------------------------- 单次巡逻
def scan_once(state=None):
    """执行一次巡逻。返回 {new, recovered, auto_remediated, baseline, scan_ok}。"""
    state = state if state is not None else load_state()
    result = {"scan_ok": False, "baseline": False, "new": [],
              "recovered": [], "auto_remediated": []}

    # 1) 拉取当前未闭环异常（感知层，只读）
    try:
        nb.login()
        sev = None if config.WATCH_SEVERITY == "all" else "danger"
        data = tools.list_active_incidents(severity=sev)
        state["scan_fail_count"] = 0
        state["scan_fail_alerted"] = False
        result["scan_ok"] = True
    except Exception as e:
        state["scan_fail_count"] = state.get("scan_fail_count", 0) + 1
        log.error("第 %d 次扫描失败：%s", state["scan_fail_count"], e)
        if (config.WATCH_SCAN_FAIL_ALERT and not state.get("scan_fail_alerted")
                and state["scan_fail_count"] >= config.WATCH_SCAN_FAIL_ALERT):
            notifier.notify("【巡逻告警】监控数据源连接异常",
                            "巡逻器连续 %d 次无法从 NetBox 获取异常清单：%s\n"
                            "请检查 NetBox 服务/网络/账号。" % (state["scan_fail_count"], e),
                            level="critical")
            state["scan_fail_alerted"] = True
        save_state(state)
        return result

    current = {str(i["incident_id"]): i for i in data["incidents"]}
    now = time.strftime("%Y-%m-%dT%H:%M:%S")

    # 2) 首巡建立基线（默认静默，避免对存量异常刷屏）
    if not state.get("baseline_done"):
        for iid, inc in current.items():
            state["incidents"][iid] = {
                "first_seen": now, "last_seen": now,
                "device": inc["device"], "category": inc["category"],
                "severity": inc["severity"], "summary": inc["summary"],
                "alerted": config.WATCH_ALERT_ON_START, "status": "open"}
        state["baseline_done"] = True
        result["baseline"] = True
        log.info("首巡完成：建立基线，当前未闭环异常 %d 条%s",
                 len(current), "（已按配置全部告警）" if config.WATCH_ALERT_ON_START else "（静默不告警）")
        if config.WATCH_ALERT_ON_START:
            for inc in current.values():
                alert_new_incident(inc)
        save_state(state)
        return result

    # 3) 新异常检测
    for iid, inc in current.items():
        rec = state["incidents"].get(iid)
        if rec is None:
            log.info("🚨 发现新异常：incident#%s %s [%s] %s",
                     iid, inc["device"], inc["category"], inc["summary"][:60])
            alert_new_incident(inc)
            new_rec = {"first_seen": now, "last_seen": now,
                       "device": inc["device"], "category": inc["category"],
                       "severity": inc["severity"], "summary": inc["summary"],
                       "alerted": True, "status": "open"}
            state["incidents"][iid] = new_rec
            result["new"].append(inc)
            # 可选：自动调 Agent 处置（无 TTY，强制免确认）
            if config.WATCH_AUTO_REMEDIATE:
                if _auto_remediate(inc):
                    new_rec["status"] = "remediated"
                    result["auto_remediated"].append(inc["device"])
        else:
            rec["last_seen"] = now  # 仍未闭环，刷新观察时间

    # 4) 恢复检测：之前告警过、现在已不在未闭环清单
    for iid, rec in list(state["incidents"].items()):
        if iid not in current and rec.get("status") in ("open", "remediated"):
            was_auto_remediated = rec.get("status") == "remediated"
            result["recovered"].append(rec)
            log.info("✅ 异常恢复：incident#%s %s [%s]%s", iid, rec["device"],
                     rec["category"], "（自动处置闭环）" if was_auto_remediated else "")
            if config.WATCH_NOTIFY_RECOVERY and rec.get("alerted"):
                alert_recovery({"device": rec["device"],
                                "auto": was_auto_remediated}, rec)
            rec["status"] = "closed"

    save_state(state)
    log.info("巡逻完成：未闭环 %d 条，新异常 %d，恢复 %d，自动处置 %d",
             len(current), len(result["new"]), len(result["recovered"]),
             len(result["auto_remediated"]))
    return result


def _auto_remediate(inc):
    """对新异常自动启动 Agent 三阶段处置。成功跑完返回 True（是否闭环由复验决定）。"""
    try:
        import agent
        config.AUTO_CONFIRM = True  # 守护进程无 TTY，自动确认（高危操作的自动处置需明确开启本开关）
        prompt = ("[巡逻自动发现] 设备 %s 发生异常：%s（severity=%s，category=%s）。"
                  "请严格按三阶段流程：先检索知识库与历史经验，再诊断，最后在确认动作合理后处置并闭环。"
                  % (inc["device"], inc["summary"][:120], inc["severity"], inc["category"]))
        log.info("🤖 启动自动处置：%s（category=%s）", inc["device"], inc["category"])
        agent.run(prompt)
        return True
    except Exception as e:
        log.error("自动处置 %s 失败：%s", inc["device"], e)
        notifier.notify("【巡逻告警】自动处置执行异常",
                        "设备 %s 自动处置过程中发生异常：%s\n请人工介入。"
                        % (inc["device"], e), level="critical")
        return False


# ---------------------------------------------------------------- 入口
def main():
    if "--once" in sys.argv:
        log.info("=== 单次巡逻开始 ===")
        r = scan_once()
        if not r["scan_ok"]:
            sys.exit(1)
        log.info("=== 单次巡逻结束：新异常 %d，恢复 %d ===",
                 len(r["new"]), len(r["recovered"]))
        return

    log.info("=== 巡逻器启动：间隔 %ds，级别=%s，自动处置=%s，IM渠道=%s ===",
             config.WATCH_INTERVAL, config.WATCH_SEVERITY,
             "开" if config.WATCH_AUTO_REMEDIATE else "关",
             notifier.configured_channels() or "未配置")
    while True:
        try:
            scan_once()
        except Exception as e:
            log.exception("巡逻循环异常（将在下个周期重试）: %s", e)
        time.sleep(config.WATCH_INTERVAL)


if __name__ == "__main__":
    main()
