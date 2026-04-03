api2cursor 日志导出包说明
========================

01_cursor_proxy_sessions/
  按日期分目录的会话 JSON（与服务器 data/conversations 下内容一致，二进制原样打包）。
  每条 turn 含 client_request / upstream_request / upstream_response / client_response / stream_trace 等，
  用于分析 Cursor → 本代理 → 上游 LLM 的交互流程。

02_application_meta/
  settings_snapshot.json — 当前持久化配置快照（data/settings.json 等价）。
  log_notes.json — 管理面板为会话添加的备注。

manifest.json — 导出元数据（时间、筛选条件、文件数量等）。

关于「流式是否截断」：若环境变量 VERBOSE_FULL_STREAM=1，则 verbose 模式下写入磁盘的
stream_trace 事件为完整列表；否则可能仅保留头尾若干条（中间折叠计数），详见 request_logger。
