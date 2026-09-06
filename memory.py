# -*- coding: utf-8 -*-
"""轻量记忆模块（零依赖，标准库实现）。

三层记忆对应关系：
- 工作记忆：agent.py 进程内的 messages / state（易失，单次处置会话）。
- 情景记忆（episodic）：memory/sessions/<session_id>.jsonl，
  逐行记录一次处置会话中的用户输入、工具调用、工具结果摘要、阶段流转、闭环/升级事件。
- 经验记忆（semantic）：memory/experiences.jsonl，
  每次闭环成功或处置失败升级时，按确定性规则沉淀一条"现象→根因→有效处置/失败尝试"经验，
  可被分析层 search_knowledge_base 检索（source=experience），让后续处置复用成功经验、避开失败动作。

运维事实状态仍以 NetBox 为唯一权威来源；记忆只存"过程与经验"，不存权威状态。
所有写入/读取均吞异常并降级，记忆模块故障绝不影响 Agent 主流程。
"""
import json
import logging
import os
import time
import uuid

import config
from knowledge_base import tokenize

log = logging.getLogger("memory")

SESSION_DIR = os.path.join(config.MEMORY_DIR, "sessions")
EXPERIENCES_FILE = os.path.join(config.MEMORY_DIR, "experiences.jsonl")

# 事件/结果落盘时的截断长度，防止 JSONL 膨胀
_MAX_ARG = 800
_MAX_RESULT = 1200
_MAX_TEXT = 600


def _enabled():
    return config.MEMORY_ENABLED


def _ensure_dirs():
    os.makedirs(SESSION_DIR, exist_ok=True)
    os.makedirs(config.MEMORY_DIR, exist_ok=True)


def _session_path(session_id):
    return os.path.join(SESSION_DIR, session_id + ".jsonl")


def _append_jsonl(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def _truncate(v, n):
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    return s if len(s) <= n else s[:n] + "…<截断>"


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


# ============================================================================
# 情景记忆：会话轨迹
# ============================================================================

def start_session(user_input=""):
    """开启一次处置会话，返回 session_id；失败返回 None。"""
    if not _enabled():
        return None
    try:
        sid = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        _ensure_dirs()
        _append_jsonl(_session_path(sid), {
            "ts": _now_iso(), "type": "session_start",
            "netbox": config.NETBOX_URL, "model": config.DEEPSEEK_MODEL,
        })
        if user_input:
            log_event(sid, "user_input", content=_truncate(user_input, _MAX_TEXT * 2))
        log.info("记忆会话开始：%s", sid)
        return sid
    except Exception as e:
        log.warning("start_session 失败（记忆降级为关闭）: %s", e)
        return None


def log_event(session_id, etype, **data):
    """向会话轨迹追加一条事件（tool_call / tool_result / stage / closed / escalated 等）。"""
    if not _enabled() or not session_id:
        return
    try:
        event = {"ts": _now_iso(), "type": etype}
        for k, v in data.items():
            event[k] = _truncate(v, _MAX_RESULT) if isinstance(v, (dict, list)) else v
        _append_jsonl(_session_path(session_id), event)
    except Exception as e:
        log.warning("log_event(%s) 失败: %s", etype, e)


def log_tool_call(session_id, name, layer, args, result):
    """记录一次工具调用及其结果摘要（args/result 截断）。"""
    log_event(session_id, "tool_call",
              tool=name, layer=layer,
              args=_truncate(args or {}, _MAX_ARG),
              result_summary=_truncate(result if isinstance(result, dict) else {"raw": str(result)},
                                       _MAX_RESULT))


# ============================================================================
# 经验记忆：闭环/升级后沉淀
# ============================================================================

def record_experience(entry):
    """沉淀一条处置经验到 experiences.jsonl。

    entry 由 agent 侧用确定性数据组装（不经过 LLM 抽取，避免幻觉）。
    返回带 id/ts 的完整条目；失败返回 None。
    """
    if not _enabled():
        return None
    try:
        _ensure_dirs()
        record = dict(entry)
        record.setdefault("id", "EXP-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])
        record.setdefault("ts", _now_iso())
        record.setdefault("failed_attempts", [])
        record.setdefault("kb_refs", [])
        record.setdefault("attempts", 0)
        _append_jsonl(EXPERIENCES_FILE, record)
        log.info("经验已沉淀：%s device=%s category=%s result=%s",
                 record["id"], record.get("device"), record.get("category"), record.get("result"))
        return record
    except Exception as e:
        log.warning("record_experience 失败: %s", e)
        return None


def load_experiences():
    if not _enabled() or not os.path.exists(EXPERIENCES_FILE):
        return []
    out = []
    try:
        with open(EXPERIENCES_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except Exception as e:
        log.warning("load_experiences 失败: %s", e)
    return out


def search_experiences(query, top_k=None):
    """关键词检索经验记忆（与知识库同一套分词；未来可替换为向量检索，仅需改本函数）。"""
    if not _enabled():
        return []
    top_k = top_k or config.MEMORY_SEARCH_TOP_K
    q_tokens = tokenize(query)
    scored = []
    for exp in load_experiences():
        eff = exp.get("effective_action") or {}
        failed = exp.get("failed_attempts") or []
        fields = [
            (str(exp.get("device", "")), 5.0),
            (str(exp.get("category", "")), 4.0),
            (str(exp.get("incident_summary", "")), 3.0),
            (str(exp.get("root_cause_hint", "")), 2.0),
            (str(eff.get("tool", "")) + " " + str(eff.get("remediation_type", "")), 3.0),
            (" ".join(str(f.get("tool", "")) for f in failed), 2.0),
            (" ".join(str(k) for k in exp.get("kb_refs", [])), 1.0),
        ]
        score = 0.0
        for text, weight in fields:
            score += len(q_tokens & tokenize(text)) * weight
        if score > 0:
            # 同类同设备更新的经验略加权（时间序在后）
            scored.append((score, exp))
    scored.sort(key=lambda x: x[0], reverse=True)

    cases = []
    for score, exp in scored[:top_k]:
        eff = exp.get("effective_action") or {}
        failed = exp.get("failed_attempts") or []
        result_zh = "闭环成功" if exp.get("result") == "closed" else "处置失败转人工"
        steps, case_lines = [], []
        if eff.get("tool"):
            steps.append("有效处置工具：%s（%s），复验通过后闭环。"
                         % (eff.get("tool"), eff.get("remediation_type")))
        else:
            steps.append("该故障未能通过远程自动处置闭环（%s），需人工/现场介入。" % result_zh)
        if exp.get("root_cause_hint"):
            steps.append("根因线索：%s" % exp["root_cause_hint"])
        if failed:
            tools_tried = "、".join(str(f.get("tool")) for f in failed)
            steps.append("⚠️ 以下动作已验证无效、已自动回滚，请勿重复：%s" % tools_tried)
            for f in failed:
                case_lines.append("失败尝试：%s —— %s" % (f.get("tool"), f.get("detail", "")))
        case_lines.append("设备 %s：%s（经验编号 %s，%s）"
                          % (exp.get("device"), exp.get("incident_summary", "")[:120],
                             exp.get("id"), exp.get("ts", "")))
        cases.append({
            "doc_id": exp.get("id"),
            "title": "【经验记忆】%s 类故障 @ %s —— %s"
                     % (exp.get("category"), exp.get("device"), result_zh),
            "category": exp.get("category"),
            "source": "experience",
            "score": round(score, 1),
            "matched_keywords": [],
            "symptoms": [exp.get("incident_summary", "")],
            "remediation_steps": steps,
            "historical_case": "；".join(case_lines),
            "recommended_action_tool": eff.get("tool"),
            "result": exp.get("result"),
            "failed_attempts": failed,
            "device": exp.get("device"),
        })
    return cases
