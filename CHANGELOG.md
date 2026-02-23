# Changelog

## v2.0.1 - 2026-02-23
- 修复：撤回状态键改为 `unified_msg_origin::message_id`，避免跨会话 message_id 冲突导致误拦截。
- 增强：UUID（带短横线）识别兼容；待处理/已撤回状态读写统一按会话维度。

## v2.0.0
- 完整重构版本（详见 git 历史）。
