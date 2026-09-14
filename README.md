# Sre_tool_agent

> 面向 SRE 场景的智能运维 Agent：以 **NetBox** 为数据面，基于 **四层工具栈** 与 **三阶段闭环状态机**，实现「发现异常 → 诊断 → 处置 → 复验 → 闭环/回滚」的全自动故障处置闭环。

<div align="center">

[![Python](https://img.shields.io/badge/Python-3.x-blue?style=flat-square&logo=python&logoColor=white)](#)
[![LLM](https://img.shields.io/badge/LLM-DeepSeek-536DFE?style=flat-square)](#)
[![NetBox](https://img.shields.io/badge/Data-Plane%20NetBox-2C7BE5?style=flat-square)](#)
[![License](https://img.shields.io/badge/License-MIT-green?style=flat-square)](#)

</div>

---

## ✨ 特性

- 🧠 **四层工具栈**：感知 / 分析 / 处置 / 通知，读写权限清晰分离
- 🔁 **三阶段闭环状态机**：DETECT → DIAGNOSE → ACT，自动复验与回滚
- 🛡️ **六步权限流转**：身份 → 设备 → 动作 → 风险分级 → 审计 → 执行
- 🔒 **高危操作二次确认**：写操作分层分级，自动快照、可回滚
- 📡 **多通道告警**：钉钉 / 飞书 / 企微机器人推送

---

## 🚀 快速开始

### 1. 配置 API Key

```bash
export DEEPSEEK_API_KEY=sk-xxxx            # 或把 key 写入工作目录 .deepseek_key
```

### 2. 运行

```bash
# 单次执行：传入故障描述
python3 agent.py "access-bj-02 疑似环路，终端大面积掉线"

# 交互模式
python3 agent.py

# 跳过二次确认（仅演示 / 测试用）
AGENT_AUTO_CONFIRM=1 python3 agent.py ...
```

---

## 🔁 三阶段闭环状态机

| 阶段 | 名称 | 行为 |
| :--: | :--- | :--- |
| 一 | **DETECT** | 发现异常 → 强制感知 + `search_knowledge_base`，禁止直接回答 |
| 二 | **DIAGNOSE** | 基于历史预案 / 变更关联，给出诊断与处置建议 |
| 三 | **ACT** | 处置工具 → 二次确认 → 执行 → `verify` 复验 |

**ACT 阶段分支：**

```
恢复    → close_incident_loop  → 「闭环完成」
未恢复  → 自动 rollback_last_action → 回到阶段二（限次后转人工）
```

---

## 🧰 四层工具栈

数据面为 **NetBox**（`localhost:8000`）。

### 1️⃣ 感知层 Perception（只读 GET）

| 工具 | 说明 |
| :--- | :--- |
| `list_active_incidents` | 拉取当前未闭环异常（journal 异常登记） |
| `get_device_detail` | 设备详情 / 状态 |
| `get_device_interfaces` | 接口状态（up / down） |
| `verify_incident_resolved` | 闭环验证：处置后重新获取异常信息，判断是否恢复 |

### 2️⃣ 分析层 Analysis（只读 + 检索）

| 工具 | 说明 |
| :--- | :--- |
| `search_knowledge_base` | 检索内部运维文档 / 历史故障预案 |
| `correlate_events` | 时间窗口内变更事件（发布 / 回退 / 割接）与异常关联比对 |

### 3️⃣ 处置层 Action（写操作 · 高危 · 必须二次确认）

> 每个动作执行前自动快照，可回滚。

| 工具 | 说明 |
| :--- | :--- |
| `shutdown_loop_port` | 定位并关闭环路端口 |
| `deploy_loop_protection` | 下发环路防护配置 |
| `adjust_buffer_queue` | 调整缓冲与 QoS 队列配置 |
| `troubleshoot_process_restart` | 远程排查进程，必要时重启 |
| `fix_oob_management` | 排查 / 修正带外管理网络配置 |
| `fix_ntp_source` | 检查并修正 NTP 源配置 |
| `tune_soft_config` | 检查 hello 计时器、CPU 阈值等软配置 |
| `fix_device_config` | 修正设备配置（VLAN / DHCP Snooping / BGP / ACL 等） |
| `rollback_config` | 回滚错误变更 / 恢复误删配置（如默认路由） |
| `rollback_last_action` | 回滚工具：按快照撤销最近一次处置动作 |

### 4️⃣ 通知层 Notify（对外推送 · 只读外部副作用）

> 不改变 NetBox 状态。

| 工具 | 说明 |
| :--- | :--- |
| `send_notification` | 通过钉钉 / 飞书 / 企微群机器人推送消息（异常告警、人工确认 / 升级、闭环通报） |

---

## 🛡️ 权限流转

```
① 身份检查：OPERATOR_ROLE 能不能调这类工具？
   ├─ viewer            → 直接拒绝
   └─ operator / admin  → 继续
                        │
                        ▼
② 设备检查：这台设备允许操作吗？
   ├─ 在黑名单 / 核心设备 → 拒绝
   └─ 在允许范围          → 继续
                        │
                        ▼
③ 动作检查：这个工具在禁止列表吗？
   ├─ blocked → 拒绝
   └─ 允许    → 继续
                        │
                        ▼
④ 风险分级确认：
   ├─ tier-1 auto_safe       → 自动放行（回滚 / 防护 / 软配置）
   ├─ tier-2 needs_confirm   → 交互模式弹 yes/no / watchdog 模式 IM 确认
   └─ tier-3 always_confirm  → 任何模式都必须人输 yes（关端口 / 重启）
                        │
                        ▼
⑤ 审计记录：谁、什么角色、什么时候、对哪台设备、用了什么工具、确认了没有
                        │
                        ▼
⑥ 执行 → 快照 → 复验 → 闭环 / 回滚
```

---

## 📁 项目结构

```
.
├── agent.py              # Agent 主入口
├── .deepseek_key         # 可选：API Key 配置（勿提交到仓库）
└── ...
```

---

## 📄 License

[MIT](LICENSE)
