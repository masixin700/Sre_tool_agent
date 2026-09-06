# Sre_tool_agent

"""
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

通知层 Notify 对外推送 / 只读外部副作用、不改变 NetBox 状态）
  - send_notification 通过钉钉/飞书/企微群机器人推送消息（异常告警、人工确认/升级、闭环通报）。
    
"""
