# -*- coding: utf-8 -*-
"""内部运维知识库：历史故障预案检索（分析层 search_knowledge_base 的后端）。

零依赖检索：英文/数字按词切分，中文按二元组（bigram）切分，
对 标题/标签/症状/步骤 做加权关键词打分。
"""
import json
import re

import config

_CJK = re.compile(r"[\u4e00-\u9fa5]+")
_ASCII_WORD = re.compile(r"[a-z0-9]+[a-z0-9\.\-/]*")


def tokenize(text):
    """中英文混合分词：英文数字按词，中文按字 + bigram。"""
    text = text.lower()
    tokens = set()
    for w in _ASCII_WORD.findall(text):
        tokens.add(w)
    for seg in _CJK.findall(text):
        for ch in seg:
            tokens.add(ch)
        for i in range(len(seg) - 1):
            tokens.add(seg[i:i + 2])
    return tokens


class KnowledgeBase:
    def __init__(self, path=config.KB_PATH):
        with open(path, encoding="utf-8") as f:
            self.docs = json.load(f)
        self._index = []
        for doc in self.docs:
            blob_fields = {
                "title": 3.0,
                "tags": 4.0,                      # 标签命中权重最高
                "symptoms": 3.0,
                "remediation_steps": 1.0,
                "historical_case": 1.0,
            }
            field_tokens = {}
            for field, weight in blob_fields.items():
                content = doc.get(field)
                text = " ".join(content) if isinstance(content, list) else str(content)
                field_tokens[field] = (tokenize(text), weight)
            self._index.append((doc, field_tokens))

    def search(self, query, top_k=3):
        q_tokens = tokenize(query)
        scored = []
        for doc, field_tokens in self._index:
            score = 0.0
            hits = []
            for field, (ftokens, weight) in field_tokens.items():
                overlap = q_tokens & ftokens
                if overlap:
                    score += len(overlap) * weight
                    if field in ("title", "tags", "symptoms"):
                        hits.extend(sorted(overlap))
            if score > 0:
                scored.append((score, doc, sorted(set(hits))))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [
            {
                "score": round(score, 1),
                "doc_id": doc["id"],
                "title": doc["title"],
                "category": doc["category"],
                "recommended_action_tool": doc["action_tool"],
                "matched_keywords": hits[:10],
                "symptoms": doc["symptoms"],
                "remediation_steps": doc["remediation_steps"],
                "historical_case": doc["historical_case"],
            }
            for score, doc, hits in scored[:top_k]
        ]


kb = KnowledgeBase()
