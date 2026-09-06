# -*- coding: utf-8 -*-
"""IM 群机器人通知：钉钉 / 飞书 / 企业微信（自定义机器人 webhook）。

用途：异常告警推送、高危处置的人工确认/升级通知、闭环结果通报。
- 零依赖，标准库 urllib + hmac 加签；
- webhook/密钥全部走环境变量（见 config.py），未配置的渠道自动跳过；
- 任何渠道失败只记录错误、不抛异常，不影响 Agent 主流程。
"""
import base64
import hashlib
import hmac
import json
import logging
import time
import urllib.parse
import urllib.request
import urllib.error

import config

log = logging.getLogger("notifier")

LEVEL_ICON = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨", "success": "✅"}

CHANNELS = ("dingtalk", "feishu", "wecom")


def _http_post_json(url, payload, timeout=None):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout or config.NOTIFY_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------- 钉钉
def _dingtalk_sign(secret, timestamp_ms):
    string_to_sign = "%s\n%s" % (timestamp_ms, secret)
    digest = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"),
                      hashlib.sha256).digest()
    return urllib.parse.quote_plus(base64.b64encode(digest))


def send_dingtalk(title, content, level="info"):
    if not config.DINGTALK_WEBHOOK:
        return {"channel": "dingtalk", "sent": False, "skipped": "未配置 DINGTALK_WEBHOOK"}
    url = config.DINGTALK_WEBHOOK
    if config.DINGTALK_SECRET:
        ts = str(round(time.time() * 1000))
        sign = _dingtalk_sign(config.DINGTALK_SECRET, ts)
        url = "%s%stimestamp=%s&sign=%s" % (url, "&" if "?" in url else "?", ts, sign)
    icon = LEVEL_ICON.get(level, "")
    text = "### %s %s\n\n%s" % (icon, title, content)
    resp = _http_post_json(url, {"msgtype": "markdown",
                                 "markdown": {"title": title, "text": text}})
    ok = resp.get("errcode", 0) == 0
    return {"channel": "dingtalk", "sent": ok,
            "error": None if ok else "errcode=%s errmsg=%s" % (resp.get("errcode"), resp.get("errmsg"))}


# ---------------------------------------------------------------- 飞书
def _feishu_sign(secret, timestamp_s):
    string_to_sign = "%s\n%s" % (timestamp_s, secret)
    # 飞书规范：以 string_to_sign 本身作为 HMAC key，消息体为空
    digest = hmac.new(string_to_sign.encode("utf-8"), b"", hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def send_feishu(title, content, level="info"):
    if not config.FEISHU_WEBHOOK:
        return {"channel": "feishu", "sent": False, "skipped": "未配置 FEISHU_WEBHOOK"}
    icon = LEVEL_ICON.get(level, "")
    payload = {"msg_type": "text",
               "content": {"text": "%s %s\n%s" % (icon, title, content)}}
    if config.FEISHU_SECRET:
        ts = str(int(time.time()))
        payload["timestamp"] = ts
        payload["sign"] = _feishu_sign(config.FEISHU_SECRET, ts)
    resp = _http_post_json(config.FEISHU_WEBHOOK, payload)
    ok = resp.get("code", 0) == 0 or resp.get("StatusCode", 0) == 0
    return {"channel": "feishu", "sent": ok,
            "error": None if ok else "code=%s msg=%s" % (resp.get("code"), resp.get("msg"))}


# ---------------------------------------------------------------- 企业微信
def send_wecom(title, content, level="info"):
    if not config.WECOM_WEBHOOK:
        return {"channel": "wecom", "sent": False, "skipped": "未配置 WECOM_WEBHOOK"}
    icon = LEVEL_ICON.get(level, "")
    markdown = "## %s %s\n\n%s" % (icon, title, content)
    resp = _http_post_json(config.WECOM_WEBHOOK,
                           {"msgtype": "markdown", "markdown": {"content": markdown}})
    ok = resp.get("errcode", 0) == 0
    return {"channel": "wecom", "sent": ok,
            "error": None if ok else "errcode=%s errmsg=%s" % (resp.get("errcode"), resp.get("errmsg"))}


_SENDERS = {"dingtalk": send_dingtalk, "feishu": send_feishu, "wecom": send_wecom}


def configured_channels():
    return [c for c in CHANNELS if {
        "dingtalk": config.DINGTALK_WEBHOOK,
        "feishu": config.FEISHU_WEBHOOK,
        "wecom": config.WECOM_WEBHOOK,
    }[c]]


def notify(title, content, channels=None, level="info"):
    """向 IM 群机器人发送通知。

    channels: None 或 "all" = 所有已配置渠道；也可传 "dingtalk"/"feishu"/"wecom" 或其列表。
    返回 {"notified": 已发送渠道数, "results": [...]}，永不抛异常。
    """
    if channels in (None, "all", ""):
        targets = configured_channels()
    elif isinstance(channels, str):
        targets = [c.strip() for c in channels.split(",") if c.strip() in CHANNELS]
    else:
        targets = [c for c in channels if c in CHANNELS]

    results, notified = [], 0
    for ch in targets:
        try:
            r = _SENDERS[ch](title, content, level=level)
        except Exception as e:
            log.warning("通知渠道 %s 发送异常: %s", ch, e)
            r = {"channel": ch, "sent": False, "error": "%s: %s" % (type(e).__name__, e)}
        results.append(r)
        notified += 1 if r.get("sent") else 0
    if not targets:
        log.info("通知跳过（未配置任何 IM webhook）：%s", title)
    else:
        log.info("通知发送完成：%s，成功 %d/%d", title, notified, len(targets))
    return {"notified": notified, "results": results,
            "configured_channels": configured_channels()}
