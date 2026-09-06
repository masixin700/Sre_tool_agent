# -*- coding: utf-8 -*-
"""全局配置：NetBox 数据源 / DeepSeek LLM。

DeepSeek API Key 读取顺序：环境变量 DEEPSEEK_API_KEY -> 工作目录 .deepseek_key 文件。
NetBox Token 启动时用账号密码通过 provision 接口自动获取，无需手工填写。
"""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ---- macOS python.org 版 Python 的 HTTPS 修复 ----
# python.org 安装的 Python 若未运行过 "Install Certificates.command"，其 CA 根证书库
# （…/etc/openssl/cert.pem）不存在，调用 https API 会报 "self-signed certificate
# in certificate chain"。此处自动回退到 macOS 系统自带 CA 库；用户显式设置了
# SSL_CERT_FILE 时不覆盖。
if os.uname().sysname == "Darwin":
    import ssl
    _p = ssl.get_default_verify_paths()
    _ca_ok = any([
        _p.cafile and os.path.exists(_p.cafile),
        _p.openssl_cafile and os.path.exists(_p.openssl_cafile),
        _p.capath and os.path.isdir(_p.capath),
        _p.openssl_capath and os.path.isdir(_p.openssl_capath),
    ])
    if not _ca_ok and not os.getenv("SSL_CERT_FILE"):
        if os.path.exists("/etc/ssl/cert.pem"):
            os.environ["SSL_CERT_FILE"] = "/etc/ssl/cert.pem"

# ---- NetBox（数据源 / 处置对象）----
NETBOX_URL = os.getenv("NETBOX_URL", "http://localhost:8000")
NETBOX_USER = os.getenv("NETBOX_USER", "admin")
NETBOX_PASSWORD = os.getenv("NETBOX_PASSWORD", "12345678")

# ---- DeepSeek（LLM，OpenAI 兼容协议）----
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()
if not DEEPSEEK_API_KEY:
    key_file = os.path.join(BASE_DIR, ".deepseek_key")
    if os.path.exists(key_file):
        DEEPSEEK_API_KEY = open(key_file, encoding="utf-8").read().strip()
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

# ---- Agent 行为 ----
AUTO_CONFIRM = os.getenv("AGENT_AUTO_CONFIRM", "0") == "1"   # 处置层动作是否免确认（仅演示用）
MAX_ACTION_ATTEMPTS = int(os.getenv("AGENT_MAX_ATTEMPTS", "3"))  # 同一异常最多处置尝试次数

# ---- 知识库 ----
KB_PATH = os.path.join(BASE_DIR, "data", "kb_docs.json")  # 本地预案库（兜底数据源）

# 外部内部运维文档知识库 API（后续对接内部 RAG/文档系统，如 Dify retrieval 接口）。
# 全部走环境变量注入，不入库不硬编码；未配置 KB_API_URL 时仅使用本地预案库。
KB_API_URL = os.getenv("KB_API_URL", "").strip()                 # 检索接口完整地址，如 https://rag.internal/api/v1/dify/retrieval
KB_API_KEY = os.getenv("KB_API_KEY", "").strip()                 # 访问令牌（Bearer）
KB_API_KNOWLEDGE_ID = os.getenv("KB_API_KNOWLEDGE_ID", "").strip()  # 知识库/数据集 ID（可选，部分系统需要）
KB_API_TIMEOUT = int(os.getenv("KB_API_TIMEOUT", "15"))          # 请求超时秒数
KB_API_AUTH_SCHEME = os.getenv("KB_API_AUTH_SCHEME", "Bearer").strip()  # 鉴权头方案：Bearer / Token / 空(不带头)

# ---- IM 机器人通知（钉钉 / 飞书 / 企业微信群机器人，均为自定义机器人 webhook）----
# 未配置任何 webhook 时通知功能自动静默；全部走环境变量，不入库。
DINGTALK_WEBHOOK = os.getenv("DINGTALK_WEBHOOK", "").strip()    # 钉钉机器人 webhook 完整地址
DINGTALK_SECRET = os.getenv("DINGTALK_SECRET", "").strip()      # 钉钉加签密钥（安全设置=加签时必填）
FEISHU_WEBHOOK = os.getenv("FEISHU_WEBHOOK", "").strip()        # 飞书机器人 webhook 完整地址
FEISHU_SECRET = os.getenv("FEISHU_SECRET", "").strip()          # 飞书签名校验密钥（可选）
WECOM_WEBHOOK = os.getenv("WECOM_WEBHOOK", "").strip()          # 企业微信群机器人 webhook 完整地址（含 key=）
NOTIFY_TIMEOUT = int(os.getenv("NOTIFY_TIMEOUT", "10"))         # 发送超时秒数
# 自动通知事件（逗号分隔）：escalation=处置失败需人工确认/介入；closed=闭环完成；anomaly=阶段一发现异常
NOTIFY_EVENTS = [e.strip() for e in os.getenv("NOTIFY_EVENTS", "escalation").split(",") if e.strip()]

# ---- 记忆模块 ----
# 工作记忆：进程内 messages/state（易失）；情景记忆：memory/sessions/*.jsonl 会话轨迹；
# 经验记忆：memory/experiences.jsonl 闭环/升级后沉淀的处置经验，可被 search_knowledge_base 检索。
MEMORY_ENABLED = os.getenv("AGENT_MEMORY", "1") == "1"
MEMORY_DIR = os.getenv("AGENT_MEMORY_DIR", os.path.join(BASE_DIR, "memory"))
MEMORY_SEARCH_TOP_K = int(os.getenv("MEMORY_SEARCH_TOP_K", "3"))  # 经验记忆检索返回条数

# ---- 定时巡逻（watchdog.py）----
# 两种用法：①python3 watchdog.py          常驻进程，每 WATCH_INTERVAL 秒巡逻一次
#          ②python3 watchdog.py --once   单次巡逻（配合 crontab/launchd 定时拉起）
WATCH_INTERVAL = int(os.getenv("WATCH_INTERVAL", "300"))       # 巡逻间隔秒数，默认 5 分钟
WATCH_SEVERITY = os.getenv("WATCH_SEVERITY", "danger").strip()  # 告警级别：danger=仅业务受损 / all=含warning风险项
WATCH_ALERT_ON_START = os.getenv("WATCH_ALERT_ON_START", "0") == "1"  # 首巡是否对存量异常也告警（默认否，首巡只建基线）
WATCH_NOTIFY_RECOVERY = os.getenv("WATCH_NOTIFY_RECOVERY", "1") == "1"  # 已告警异常恢复时是否发恢复通知
WATCH_AUTO_REMEDIATE = os.getenv("WATCH_AUTO_REMEDIATE", "0") == "1"  # 新异常是否自动调 Agent 处置（默认仅告警，由人工确认后处理）
WATCH_SCAN_FAIL_ALERT = int(os.getenv("WATCH_SCAN_FAIL_ALERT", "3"))  # 连续扫描失败 N 次后告警一次（0=不告警）
WATCH_STATE_FILE = os.getenv("WATCH_STATE_FILE",
                             os.path.join(MEMORY_DIR, "patrol_state.json"))  # 巡逻去重状态文件
