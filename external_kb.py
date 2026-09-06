# -*- coding: utf-8 -*-
"""外部内部运维文档知识库 API 客户端（分析层 search_knowledge_base 的 API 数据源）。

用途：后续对接内部 RAG / 文档系统（如 Dify retrieval 风格接口）。
- 零依赖，标准库 urllib；
- 地址、令牌、知识库 ID 全部由环境变量注入（见 config.py），未配置 KB_API_URL 时不启用；
- 请求体默认采用 Dify retrieval 风格，内部系统字段不同时可直接改 build_payload()；
- 响应解析保守：兼容 records/results/data 等常见结构，无法识别时原样回传 JSON 片段，
  不臆测字段（待拿到真实响应样例后再做字段级抽取）。
"""
import json
import logging
import urllib.request
import urllib.error

import config

log = logging.getLogger("kb_api")


def is_configured():
    """外部知识库 API 是否已配置（至少需要 URL）。"""
    return bool(config.KB_API_URL)


def build_payload(query, top_k):
    """构造检索请求体（Dify retrieval 风格默认；对接内部系统时按其契约调整本函数即可）。"""
    payload = {
        "query": query,
        "retrieval_setting": {
            "top_k": top_k,
            "score_threshold": 0.2,
        },
    }
    if config.KB_API_KNOWLEDGE_ID:
        payload["knowledge_base_id"] = config.KB_API_KNOWLEDGE_ID
    return payload


def _extract_records(data):
    """从多种常见响应结构中保守地提取检索记录列表。"""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("records", "results", "items", "docs", "documents"):
            v = data.get(key)
            if isinstance(v, list):
                return v
        d = data.get("data")
        if isinstance(d, dict):
            return _extract_records(d)
        if isinstance(d, list):
            return d
    return []


def _normalize_record(item, idx):
    """把一条记录归一化为 {title, content, score, source_ref}；无法识别则保留原文。"""
    if not isinstance(item, dict):
        return {"title": "外部文档#%d" % (idx + 1), "content": str(item)[:2000],
                "score": None, "source_ref": "api"}
    meta = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    title = (item.get("title") or item.get("document_name") or item.get("name")
             or meta.get("title") or meta.get("document_name") or "外部文档#%d" % (idx + 1))
    content = (item.get("content") or item.get("snippet") or item.get("text")
               or item.get("page_content") or item.get("chunk") or "")
    if not content:
        # 兜底：内容字段无法识别时，原样回传该记录 JSON，避免信息丢失
        content = json.dumps(item, ensure_ascii=False)[:2000]
    score = item.get("score")
    if score is None:
        score = item.get("relevance") or item.get("similarity")
    return {
        "title": str(title)[:200],
        "content": str(content)[:3000],
        "score": round(float(score), 4) if isinstance(score, (int, float)) else None,
        "source_ref": "api:" + str(item.get("document_id") or item.get("id") or meta.get("id") or idx),
    }


def search_external(query, top_k=3):
    """调用外部知识库 API 检索。

    返回 {"source":"api", "configured":..., "cases":[...]} 或 {"source":"api","error":...}，
    不抛异常——调用方（工具层）据此决定回退本地库。
    """
    if not is_configured():
        return {"source": "api", "configured": False,
                "message": "外部知识库 API 未配置（设置环境变量 KB_API_URL / KB_API_KEY / KB_API_KNOWLEDGE_ID 后启用）",
                "cases": []}
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if config.KB_API_KEY and config.KB_API_AUTH_SCHEME:
        headers["Authorization"] = "%s %s" % (config.KB_API_AUTH_SCHEME, config.KB_API_KEY)
    body = json.dumps(build_payload(query, top_k)).encode("utf-8")
    req = urllib.request.Request(config.KB_API_URL, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=config.KB_API_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:300]
        log.warning("外部知识库 API HTTP %s: %s", e.code, detail)
        return {"source": "api", "configured": True, "cases": [],
                "error": "HTTP %s: %s" % (e.code, detail)}
    except Exception as e:  # 超时 / 连接失败 / JSON 解析失败
        log.warning("外部知识库 API 调用失败: %s", e)
        return {"source": "api", "configured": True, "cases": [],
                "error": "%s: %s" % (type(e).__name__, e)}

    records = _extract_records(data)
    cases = []
    for idx, item in enumerate(records[:top_k]):
        rec = _normalize_record(item, idx)
        cases.append({
            "doc_id": rec["source_ref"],
            "title": rec["title"],
            "content": rec["content"],
            "score": rec["score"],
            "source": "api",
        })
    return {"source": "api", "configured": True, "hit_count": len(cases),
            "endpoint": config.KB_API_URL, "cases": cases,
            "raw_response_type": type(data).__name__}
