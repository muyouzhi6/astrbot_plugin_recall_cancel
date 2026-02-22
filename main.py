"""
AstrBot 撤回取消回复插件 v2.0.0 (Recall Cancel)

当用户撤回消息时，自动取消正在处理的 LLM 回复，防止 Bot 回复已撤回的消息。

核心功能:
- 撤回检测: 监听 QQ 群聊/私聊消息撤回事件
- LLM 拦截: 在多个阶段检查撤回状态并阻止回复
- 上下文清理: 同时清理 context_aware 插件中已记录的消息（如已安装）

v2.0.0 重构:
- 修复消息ID匹配问题：正确从撤回事件中提取被撤回消息的原始ID
- 修复撤回事件监听：使用正确的事件过滤器
- 新增 context_aware 集成：撤回时同步清理 context_aware 中的消息记录
- 提高事件处理优先级：确保在其他插件之前处理撤回
- 增强日志：详细的调试信息

Author: 木有知
Version: 2.0.0
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from astrbot import logger
from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.core.message.message_event_result import MessageChain

if TYPE_CHECKING:
    pass


# ============================================================================
# Constants
# ============================================================================

# 撤回事件的 notice_type
NOTICE_GROUP_RECALL: Final = "group_recall"
NOTICE_FRIEND_RECALL: Final = "friend_recall"

# 记录过期时间（秒）- 消息ID在此时间后被清理
RECORD_EXPIRE_SECONDS: Final = 300  # 5 分钟

# 清理间隔（秒）
CLEANUP_INTERVAL: Final = 60


# ============================================================================
# Data Structures
# ============================================================================


@dataclass(slots=True)
class PendingRequest:
    """正在处理的 LLM 请求记录"""
    message_id: str  # 原始消息 ID
    unified_msg_origin: str  # 会话标识
    sender_id: str  # 发送者 ID
    timestamp: float  # 请求时间戳
    event: AstrMessageEvent | None = None  # 事件引用（用于 stop_event）


@dataclass(slots=True)
class RecalledMessage:
    """已撤回的消息记录"""
    message_id: str  # 被撤回消息的 ID
    unified_msg_origin: str  # 会话标识
    operator_id: str  # 撤回操作者 ID
    timestamp: float  # 撤回时间戳
    cleaned_context_aware: bool = False  # 是否已清理 context_aware


@dataclass
class PluginStats:
    """插件统计信息"""
    recalls_detected: int = 0  # 检测到的撤回次数
    llm_requests_blocked: int = 0  # 阻止的 LLM 请求次数
    llm_responses_blocked: int = 0  # 阻止的 LLM 响应次数
    send_blocked: int = 0  # 阻止的发送次数
    context_aware_cleaned: int = 0  # 清理的 context_aware 记录次数


# ============================================================================
# Recall State Manager
# ============================================================================


class RecallStateManager:
    """撤回状态管理器 - 线程安全的状态存储"""
    
    __slots__ = ("_pending_requests", "_recalled_messages", "_lock")
    
    def __init__(self) -> None:
        self._pending_requests: dict[str, PendingRequest] = {}
        self._recalled_messages: dict[str, RecalledMessage] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _compose_key(unified_msg_origin: str, message_id: str) -> str:
        return f"{unified_msg_origin}::{message_id}"
    
    async def add_pending_request(
        self,
        message_id: str,
        unified_msg_origin: str,
        sender_id: str,
        event: AstrMessageEvent | None = None,
    ) -> None:
        """添加待处理的 LLM 请求"""
        key = self._compose_key(unified_msg_origin, message_id)
        async with self._lock:
            self._pending_requests[key] = PendingRequest(
                message_id=message_id,
                unified_msg_origin=unified_msg_origin,
                sender_id=sender_id,
                timestamp=time.time(),
                event=event,
            )
    
    async def remove_pending_request(
        self, message_id: str, unified_msg_origin: str
    ) -> PendingRequest | None:
        """移除待处理的请求"""
        key = self._compose_key(unified_msg_origin, message_id)
        async with self._lock:
            return self._pending_requests.pop(key, None)
    
    async def get_pending_request(
        self, message_id: str, unified_msg_origin: str
    ) -> PendingRequest | None:
        """获取待处理的请求"""
        key = self._compose_key(unified_msg_origin, message_id)
        async with self._lock:
            return self._pending_requests.get(key)
    
    async def add_recalled_message(
        self,
        message_id: str,
        unified_msg_origin: str,
        operator_id: str,
    ) -> None:
        """添加已撤回的消息记录"""
        key = self._compose_key(unified_msg_origin, message_id)
        async with self._lock:
            self._recalled_messages[key] = RecalledMessage(
                message_id=message_id,
                unified_msg_origin=unified_msg_origin,
                operator_id=operator_id,
                timestamp=time.time(),
            )
    
    async def is_recalled(self, message_id: str, unified_msg_origin: str) -> bool:
        """检查消息是否已被撤回"""
        key = self._compose_key(unified_msg_origin, message_id)
        async with self._lock:
            return key in self._recalled_messages
    
    async def get_recalled_message(
        self, message_id: str, unified_msg_origin: str
    ) -> RecalledMessage | None:
        """获取撤回记录"""
        key = self._compose_key(unified_msg_origin, message_id)
        async with self._lock:
            return self._recalled_messages.get(key)
    
    async def mark_context_aware_cleaned(
        self, message_id: str, unified_msg_origin: str
    ) -> None:
        """标记 context_aware 已清理"""
        key = self._compose_key(unified_msg_origin, message_id)
        async with self._lock:
            if key in self._recalled_messages:
                self._recalled_messages[key].cleaned_context_aware = True
    
    async def cleanup_expired(self, expire_seconds: float = RECORD_EXPIRE_SECONDS) -> int:
        """清理过期记录，返回清理数量"""
        now = time.time()
        cleaned = 0
        async with self._lock:
            # 清理过期的待处理请求
            expired_pending = [
                k for k, v in self._pending_requests.items()
                if now - v.timestamp > expire_seconds
            ]
            for k in expired_pending:
                del self._pending_requests[k]
                cleaned += 1
            
            # 清理过期的撤回记录
            expired_recalled = [
                k for k, v in self._recalled_messages.items()
                if now - v.timestamp > expire_seconds
            ]
            for k in expired_recalled:
                del self._recalled_messages[k]
                cleaned += 1
        
        return cleaned
    
    async def get_stats(self) -> tuple[int, int]:
        """获取当前记录数量 (pending, recalled)"""
        async with self._lock:
            return len(self._pending_requests), len(self._recalled_messages)


# ============================================================================
# Context Aware Integration
# ============================================================================


class ContextAwareIntegration:
    """context_aware 插件集成 - 负责清理已撤回消息的上下文记录"""
    
    __slots__ = ("_context", "_plugin_instance", "_checked")
    
    def __init__(self, context: star.Context) -> None:
        self._context = context
        self._plugin_instance: Any = None
        self._checked = False
    
    def _get_plugin(self) -> Any:
        """获取 context_aware 插件实例"""
        if self._checked:
            return self._plugin_instance
        
        self._checked = True
        try:
            # 尝试从已加载的插件中获取 context_aware
            for star_instance in self._context.get_all_stars():
                # 检查是否有 remove_message 方法（context_aware v2.5.1+ 提供的公开 API）
                if hasattr(star_instance, 'remove_message') and hasattr(star_instance, 'remove_last_bot_response'):
                    module_name = star_instance.__class__.__module__
                    if 'context_aware' in module_name:
                        self._plugin_instance = star_instance
                        logger.info("[RecallCancel] 已检测到 context_aware 插件，将同步清理上下文")
                        return self._plugin_instance
        except Exception as e:
            logger.debug(f"[RecallCancel] 检查 context_aware 插件时出错: {e}")
        
        return None
    
    def remove_message(self, unified_msg_origin: str, message_id: str) -> bool:
        """从 context_aware 中删除指定消息
        
        Returns:
            是否成功删除
        """
        plugin = self._get_plugin()
        if plugin is None:
            return False
        
        try:
            # 使用 context_aware 的公开 API
            result = plugin.remove_message(unified_msg_origin, message_id)
            if result:
                logger.debug(
                    f"[RecallCancel] 已从 context_aware 删除消息 "
                    f"(msg_id={message_id})"
                )
            return result
        except Exception as e:
            logger.warning(f"[RecallCancel] 清理 context_aware 失败: {e}")
        
        return False
    
    def remove_last_bot_response(self, unified_msg_origin: str) -> bool:
        """删除 context_aware 中最后一条 Bot 响应
        
        用于撤回时同时删除 Bot 可能已记录的响应。
        
        Returns:
            是否成功删除
        """
        plugin = self._get_plugin()
        if plugin is None:
            return False
        
        try:
            # 使用 context_aware 的公开 API
            result = plugin.remove_last_bot_response(unified_msg_origin)
            if result:
                logger.debug("[RecallCancel] 已从 context_aware 删除最后一条 Bot 响应")
            return result
        except Exception as e:
            logger.warning(f"[RecallCancel] 清理 context_aware Bot 响应失败: {e}")
        
        return False


# ============================================================================
# Main Plugin
# ============================================================================


class Main(star.Star):
    """
    撤回取消回复插件
    
    当用户撤回消息时，自动取消正在处理的 LLM 回复。
    支持与 context_aware 插件联动，同步清理上下文记录。
    """
    
    def __init__(self, context: star.Context) -> None:
        super().__init__(context)
        
        self._state = RecallStateManager()
        self._stats = PluginStats()
        self._context_aware = ContextAwareIntegration(context)
        self._cleanup_task: asyncio.Task | None = None
        
        logger.info("[RecallCancel] 插件 v2.0.0 已加载")
    
    # -------------------------------------------------------------------------
    # 消息 ID 提取
    # -------------------------------------------------------------------------
    
    def _get_message_id(self, event: AstrMessageEvent) -> str | None:
        """从事件中提取原始消息 ID
        
        对于普通消息：message_obj.message_id
        对于撤回事件：raw_message 中的 message_id
        """
        try:
            # 优先从 raw_message 获取（撤回事件的情况）
            raw = getattr(event.message_obj, 'raw_message', None)
            if raw:
                if isinstance(raw, dict):
                    msg_id = raw.get('message_id')
                    if msg_id:
                        return str(msg_id)
                elif hasattr(raw, 'message_id'):
                    msg_id = getattr(raw, 'message_id', None)
                    if msg_id:
                        return str(msg_id)
            
            # 回退到 message_obj.message_id
            msg_id = getattr(event.message_obj, 'message_id', None)
            if msg_id:
                # 检查是否为 UUID（撤回事件会生成新 UUID，需要排除）
                msg_id_str = str(msg_id)
                # UUID 格式检测（32位十六进制，允许带短横线）
                compact = msg_id_str.replace("-", "")
                if len(compact) == 32 and compact.isalnum():
                    # 可能是 UUID，尝试从 raw_message 获取真实 ID
                    return None
                return msg_id_str
                
        except Exception as e:
            logger.debug(f"[RecallCancel] 提取消息ID失败: {e}")
        
        return None
    
    def _is_recall_event(self, event: AstrMessageEvent) -> tuple[bool, str | None, str | None]:
        """检查是否为撤回事件
        
        Returns:
            (is_recall, recalled_message_id, operator_id)
        """
        try:
            raw = getattr(event.message_obj, 'raw_message', None)
            if not raw:
                return False, None, None
            
            # 获取 notice_type
            notice_type = None
            if isinstance(raw, dict):
                notice_type = raw.get('notice_type')
                post_type = raw.get('post_type')
            elif hasattr(raw, 'notice_type'):
                notice_type = getattr(raw, 'notice_type', None)
                post_type = getattr(raw, 'post_type', None)
            else:
                return False, None, None
            
            # 检查是否为撤回事件
            if notice_type not in (NOTICE_GROUP_RECALL, NOTICE_FRIEND_RECALL):
                return False, None, None
            
            # 提取被撤回消息的 ID
            if isinstance(raw, dict):
                recalled_msg_id = raw.get('message_id')
                operator_id = raw.get('operator_id') or raw.get('user_id')
            else:
                recalled_msg_id = getattr(raw, 'message_id', None)
                operator_id = getattr(raw, 'operator_id', None) or getattr(raw, 'user_id', None)
            
            if recalled_msg_id:
                return True, str(recalled_msg_id), str(operator_id) if operator_id else None
            
        except Exception as e:
            logger.debug(f"[RecallCancel] 检查撤回事件失败: {e}")
        
        return False, None, None
    
    # -------------------------------------------------------------------------
    # Event Handlers - 撤回事件监听
    # -------------------------------------------------------------------------
    
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.ALL, priority=100)
    async def on_all_message(self, event: AstrMessageEvent) -> None:
        """监听所有消息，检测撤回事件
        
        撤回事件在 aiocqhttp 中以 notice 类型传入，
        会被转换为 OTHER_MESSAGE 类型的 AstrMessageEvent。
        """
        # 检查是否为撤回事件
        is_recall, recalled_msg_id, operator_id = self._is_recall_event(event)
        
        if not is_recall or not recalled_msg_id:
            return
        
        self._stats.recalls_detected += 1
        umo = event.unified_msg_origin
        
        logger.info(
            f"[RecallCancel] 检测到撤回事件 | "
            f"消息ID: {recalled_msg_id} | "
            f"操作者: {operator_id} | "
            f"会话: {umo}"
        )
        
        # 记录撤回状态
        await self._state.add_recalled_message(
            message_id=recalled_msg_id,
            unified_msg_origin=umo,
            operator_id=operator_id or "",
        )
        
        # 检查是否有正在处理的 LLM 请求
        pending = await self._state.get_pending_request(recalled_msg_id, umo)
        if pending and pending.event:
            logger.info(
                f"[RecallCancel] 找到待处理的 LLM 请求，正在取消 | "
                f"消息ID: {recalled_msg_id}"
            )
            # 立即停止事件
            pending.event.stop_event()
            self._stats.llm_requests_blocked += 1
        
        # 清理 context_aware 中的消息
        if self._context_aware.remove_message(umo, recalled_msg_id):
            self._stats.context_aware_cleaned += 1
            await self._state.mark_context_aware_cleaned(recalled_msg_id, umo)
        
        # 同时删除可能已记录的 Bot 响应
        self._context_aware.remove_last_bot_response(umo)
        
        # 阻止事件继续传播（撤回事件不需要其他处理）
        event.stop_event()
    
    # -------------------------------------------------------------------------
    # Event Handlers - LLM 请求/响应拦截
    # -------------------------------------------------------------------------
    
    @filter.on_llm_request(priority=100)  # 高优先级，确保最先执行
    async def on_llm_request(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """在 LLM 请求前检查消息是否已被撤回"""
        msg_id = self._get_message_id(event)
        if not msg_id:
            return
        
        umo = event.unified_msg_origin
        sender_id = event.get_sender_id()
        
        # 记录待处理请求
        await self._state.add_pending_request(
            message_id=msg_id,
            unified_msg_origin=umo,
            sender_id=sender_id,
            event=event,
        )
        
        # 检查是否已被撤回
        if await self._state.is_recalled(msg_id, umo):
            logger.info(
                f"[RecallCancel] LLM 请求阶段拦截 | "
                f"消息已被撤回，阻止请求 | 消息ID: {msg_id}"
            )
            event.stop_event()
            self._stats.llm_requests_blocked += 1
            return
        
        logger.debug(f"[RecallCancel] 记录 LLM 请求 | 消息ID: {msg_id}")
    
    @filter.on_llm_response(priority=100)  # 高优先级
    async def on_llm_response(
        self, event: AstrMessageEvent, resp: LLMResponse
    ) -> None:
        """在 LLM 响应后检查消息是否已被撤回"""
        msg_id = self._get_message_id(event)
        if not msg_id:
            return
        umo = event.unified_msg_origin
        
        # 检查是否已被撤回
        if await self._state.is_recalled(msg_id, umo):
            logger.info(
                f"[RecallCancel] LLM 响应阶段拦截 | "
                f"消息已被撤回，阻止响应 | 消息ID: {msg_id}"
            )
            event.stop_event()
            self._stats.llm_responses_blocked += 1
            
            # 清理 context_aware 中可能已记录的 Bot 响应
            self._context_aware.remove_last_bot_response(umo)
    
    @filter.on_decorating_result(priority=100)  # 高优先级
    async def on_decorating_result(self, event: AstrMessageEvent) -> None:
        """在发送消息前最后检查一次"""
        msg_id = self._get_message_id(event)
        if not msg_id:
            return
        umo = event.unified_msg_origin
        
        # 短暂延迟，给撤回事件更多时间传入
        await asyncio.sleep(0.1)
        
        # 检查是否已被撤回
        if await self._state.is_recalled(msg_id, umo):
            logger.info(
                f"[RecallCancel] 发送阶段拦截 | "
                f"消息已被撤回，阻止发送 | 消息ID: {msg_id}"
            )
            event.stop_event()
            self._stats.send_blocked += 1
            
            # 清理 context_aware
            self._context_aware.remove_last_bot_response(umo)
    
    @filter.after_message_sent(priority=100)
    async def after_message_sent(self, event: AstrMessageEvent) -> None:
        """消息发送后清理待处理记录"""
        msg_id = self._get_message_id(event)
        if not msg_id:
            return
        
        # 移除待处理记录
        await self._state.remove_pending_request(msg_id, event.unified_msg_origin)
        logger.debug(f"[RecallCancel] 消息已发送，清理记录 | 消息ID: {msg_id}")
    
    # -------------------------------------------------------------------------
    # Background Cleanup
    # -------------------------------------------------------------------------
    
    @filter.on_astrbot_loaded()
    async def on_loaded(self, *args: Any, **kwargs: Any) -> None:
        """AstrBot 加载完成后启动清理任务"""
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())
            logger.debug("[RecallCancel] 后台清理任务已启动")
    
    async def _cleanup_loop(self) -> None:
        """定期清理过期记录"""
        while True:
            try:
                await asyncio.sleep(CLEANUP_INTERVAL)
                cleaned = await self._state.cleanup_expired()
                if cleaned > 0:
                    pending, recalled = await self._state.get_stats()
                    logger.debug(
                        f"[RecallCancel] 已清理 {cleaned} 条过期记录 | "
                        f"当前: 待处理 {pending}, 已撤回 {recalled}"
                    )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[RecallCancel] 清理任务出错: {e}")
    
    # -------------------------------------------------------------------------
    # Stats Command
    # -------------------------------------------------------------------------
    
    @filter.command("recall_stats")
    async def stats_command(self, event: AstrMessageEvent) -> None:
        """显示撤回取消插件统计信息"""
        pending, recalled = await self._state.get_stats()
        
        stats_text = (
            "📊 撤回取消插件统计\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"检测撤回: {self._stats.recalls_detected} 次\n"
            f"阻止请求: {self._stats.llm_requests_blocked} 次\n"
            f"阻止响应: {self._stats.llm_responses_blocked} 次\n"
            f"阻止发送: {self._stats.send_blocked} 次\n"
            f"清理上下文: {self._stats.context_aware_cleaned} 次\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"当前待处理: {pending} 条\n"
            f"当前撤回记录: {recalled} 条"
        )
        
        await event.send(MessageChain([Plain(stats_text)]))
    
    # -------------------------------------------------------------------------
    # Cleanup
    # -------------------------------------------------------------------------
    
    async def terminate(self) -> None:
        """清理资源"""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        
        logger.info(
            f"[RecallCancel] 插件已终止 | "
            f"统计: 检测撤回 {self._stats.recalls_detected}, "
            f"阻止请求 {self._stats.llm_requests_blocked}, "
            f"阻止响应 {self._stats.llm_responses_blocked}, "
            f"阻止发送 {self._stats.send_blocked}, "
            f"清理上下文 {self._stats.context_aware_cleaned}"
        )
