import asyncio
from typing import Dict, Any, Optional
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register


@register(
    "astrbot_plugin_recall_cancel",
    "木有知",
    "撤了就当没发，当用户撤回触发LLM回应的消息时，如果LLM回复还未发送则取消发送。防止用户发错消息撤回后机器人仍然回复的情况，提升用户体验并避免资源浪费。",
    "v1.2.0",
)
class RecallCancelPlugin(Star):
    """消息撤回取消插件

    当用户撤回触发LLM回应的消息时，如果LLM的回复还没发送出去，就取消发送。
    这能防止用户发错了消息撤回了但是astrbot还傻乎乎的回复，或者有人恶意发了一大串消息后撤回的情况。
    """

    def __init__(self, context: Context):
        super().__init__(context)

        # 存储正在处理的LLM请求
        # 键: 原始消息ID (来自平台的真实消息ID)
        # 值: {session_id, event, timestamp, cancelled, user_id, group_id, is_private}
        self.pending_llm_requests: Dict[str, Dict[str, Any]] = {}

        # 存储已撤回的消息ID集合，用于快速查找
        # 键: 原始消息ID
        # 值: {timestamp, user_id, group_id, is_private}
        self.recalled_messages: Dict[str, Dict[str, Any]] = {}

        # 清理任务
        self.cleanup_task: Optional[asyncio.Task] = None

        logger.info("RecallCancelPlugin v1.2.0 已加载")

    def _get_original_message_id(self, event: AstrMessageEvent) -> Optional[str]:
        """从事件中获取原始消息ID

        对于普通消息事件，message_obj.message_id 就是原始ID
        对于通知事件，需要从 raw_message 中获取
        """
        raw_message = event.message_obj.raw_message

        # 尝试从 raw_message 获取原始消息ID
        if raw_message:
            try:
                if hasattr(raw_message, "__getitem__"):
                    msg_id = raw_message.get("message_id") if hasattr(raw_message, "get") else raw_message["message_id"]
                    if msg_id:
                        return str(msg_id)
                elif hasattr(raw_message, "message_id"):
                    if raw_message.message_id:
                        return str(raw_message.message_id)
            except (KeyError, TypeError, AttributeError):
                pass

        # 回退到 message_obj.message_id
        if event.message_obj.message_id:
            return str(event.message_obj.message_id)

        return None

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self):
        """AstrBot加载完成后启动清理任务"""
        self.cleanup_task = asyncio.create_task(self._cleanup_expired_records())

    @filter.on_llm_request(priority=1)
    async def track_llm_request(self, event: AstrMessageEvent, req):
        """跟踪LLM请求开始"""
        message_id = self._get_original_message_id(event)
        if not message_id:
            return

        current_time = asyncio.get_running_loop().time()

        # 检查该消息是否已经被撤回
        if message_id in self.recalled_messages:
            logger.info(f"[撤回取消] LLM请求的消息已被撤回: {message_id}")
            event.stop_event()
            return

        # 获取发送者和会话信息
        sender_id = str(event.get_sender_id()) if event.get_sender_id() else ""
        group_id = str(event.get_group_id()) if event.get_group_id() else ""
        is_private = event.is_private_chat()

        # 记录LLM请求
        self.pending_llm_requests[message_id] = {
            "session_id": event.unified_msg_origin,
            "event": event,
            "timestamp": current_time,
            "cancelled": False,
            "user_id": sender_id,
            "group_id": group_id,
            "is_private": is_private,
        }
        logger.debug(f"[撤回取消] 记录LLM请求: {message_id}")

    @filter.on_llm_response(priority=1)
    async def track_llm_response(self, event: AstrMessageEvent, resp):
        """跟踪LLM响应完成"""
        message_id = self._get_original_message_id(event)
        if not message_id:
            return

        if message_id in self.pending_llm_requests:
            # 检查是否已被撤回
            if self.pending_llm_requests[message_id].get("cancelled", False):
                logger.info(f"[撤回取消] LLM响应已被撤回取消: {message_id}")
                event.stop_event()
                self.pending_llm_requests.pop(message_id, None)
                return

            logger.debug(f"[撤回取消] LLM响应已生成，等待发送: {message_id}")

    @filter.on_decorating_result(priority=1)
    async def check_before_send(self, event: AstrMessageEvent):
        """在消息发送前最后检查是否已被撤回"""
        message_id = self._get_original_message_id(event)
        if not message_id:
            return

        # 如果不在记录中，直接返回
        if message_id not in self.pending_llm_requests:
            return

        # 第一次检查
        if self.pending_llm_requests[message_id].get("cancelled", False):
            logger.info(f"[撤回取消] 发送前检测到撤回: {message_id}")
            event.stop_event()
            self.pending_llm_requests.pop(message_id, None)
            return

        # 增加短暂延迟，处理撤回事件可能稍晚到达的情况
        try:
            await asyncio.sleep(0.3)

            # 再次检查 key 是否存在
            if message_id not in self.pending_llm_requests:
                return

            # 第二次检查
            if self.pending_llm_requests[message_id].get("cancelled", False):
                logger.info(f"[撤回取消] 发送前(延迟后)检测到撤回: {message_id}")
                event.stop_event()
                self.pending_llm_requests.pop(message_id, None)
                return
        except Exception as e:
            logger.warning(f"[撤回取消] 检查过程出现异常: {e}")

    @filter.after_message_sent(priority=1)
    async def clean_sent_message(self, event: AstrMessageEvent):
        """消息发送后清理记录"""
        message_id = self._get_original_message_id(event)
        if not message_id:
            return

        if message_id in self.pending_llm_requests:
            self.pending_llm_requests.pop(message_id, None)
            logger.debug(f"[撤回取消] 清理已发送消息的记录: {message_id}")

    @filter.command("recall_status", alias={"撤回状态"})
    async def show_status(self, event: AstrMessageEvent):
        """显示插件状态 - 用于调试"""
        pending_count = len(self.pending_llm_requests)
        recalled_count = len(self.recalled_messages)

        status_msg = "撤回取消插件状态:\n"
        status_msg += f"待处理LLM请求: {pending_count}\n"
        status_msg += f"已记录撤回消息: {recalled_count}\n"
        status_msg += f"清理任务: {'运行中' if self.cleanup_task and not self.cleanup_task.done() else '已停止'}"

        if pending_count > 0:
            status_msg += "\n\n当前待处理请求:"
            for msg_id in list(self.pending_llm_requests.keys())[:5]:
                info = self.pending_llm_requests[msg_id]
                cancelled = "已取消" if info.get("cancelled") else "等待中"
                status_msg += f"\n- {msg_id[:16]}... ({cancelled})"
            if pending_count > 5:
                status_msg += f"\n- ... 还有 {pending_count - 5} 个"

        yield event.plain_result(status_msg)

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.ALL, priority=1)
    async def handle_recall_event(self, event: AstrMessageEvent):
        """处理消息撤回事件（OneBot V11标准）"""
        raw_message = event.message_obj.raw_message
        if not raw_message:
            return

        try:
            # 统一获取值的方法
            def get_value(obj, key, default=None):
                try:
                    if hasattr(obj, "get"):
                        return obj.get(key, default)
                    elif hasattr(obj, "__getitem__"):
                        return obj[key]
                except (KeyError, TypeError):
                    pass
                return getattr(obj, key, default)

            post_type = get_value(raw_message, "post_type")
            notice_type = get_value(raw_message, "notice_type")

            # 检查是否是撤回事件
            if post_type != "notice" or notice_type not in ["group_recall", "friend_recall"]:
                return

            # 获取被撤回消息的原始ID（这是关键！）
            recalled_message_id = get_value(raw_message, "message_id")
            if not recalled_message_id:
                logger.warning(f"[撤回取消] 撤回事件中没有message_id: {raw_message}")
                return

            recalled_message_id = str(recalled_message_id)

            # 获取上下文信息
            user_id = str(get_value(raw_message, "user_id", ""))
            group_id = str(get_value(raw_message, "group_id", ""))
            operator_id = str(get_value(raw_message, "operator_id", ""))
            is_private = notice_type == "friend_recall"

            logger.info(f"[撤回取消] 检测到消息撤回: {recalled_message_id} (类型: {notice_type}, 用户: {user_id})")

            # 记录撤回的消息
            current_time = asyncio.get_running_loop().time()
            self.recalled_messages[recalled_message_id] = {
                "timestamp": current_time,
                "user_id": user_id,
                "group_id": group_id,
                "operator_id": operator_id,
                "is_private": is_private
            }

            # 精确匹配：查找对应的LLM请求
            if recalled_message_id in self.pending_llm_requests:
                request_info = self.pending_llm_requests[recalled_message_id]
                request_info["cancelled"] = True

                # 尝试停止相关事件
                if "event" in request_info:
                    try:
                        request_info["event"].stop_event()
                    except Exception as e:
                        logger.debug(f"[撤回取消] 停止事件时出错: {e}")

                logger.info(f"[撤回取消] 已取消对应的LLM回复: {recalled_message_id}")
            else:
                logger.debug(f"[撤回取消] 撤回的消息没有对应的LLM请求: {recalled_message_id}")

            # 阻止此撤回事件继续传播（避免触发其他处理）
            event.stop_event()

        except Exception as e:
            logger.error(f"[撤回取消] 处理撤回事件时出现异常: {e}", exc_info=True)

    async def _cleanup_expired_records(self):
        """定期清理过期的记录"""
        while True:
            try:
                await asyncio.sleep(300)  # 每5分钟清理一次
                current_time = asyncio.get_running_loop().time()

                # 清理超过10分钟的LLM请求记录
                expired_requests = [
                    msg_id for msg_id, info in self.pending_llm_requests.items()
                    if current_time - info["timestamp"] > 600
                ]
                for msg_id in expired_requests:
                    self.pending_llm_requests.pop(msg_id, None)

                if expired_requests:
                    logger.debug(f"[撤回取消] 清理了 {len(expired_requests)} 个过期LLM请求记录")

                # 清理超过2分钟的撤回消息记录
                expired_recalls = [
                    msg_id for msg_id, info in self.recalled_messages.items()
                    if current_time - info["timestamp"] > 120
                ]
                for msg_id in expired_recalls:
                    self.recalled_messages.pop(msg_id, None)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[撤回取消] 清理任务异常: {e}")

    async def terminate(self):
        """插件卸载时的清理工作"""
        if self.cleanup_task and not self.cleanup_task.done():
            self.cleanup_task.cancel()
            try:
                await self.cleanup_task
            except asyncio.CancelledError:
                pass

        self.pending_llm_requests.clear()
        self.recalled_messages.clear()
        logger.info("RecallCancelPlugin 已卸载")


# 为了向后兼容，保留Main类
Main = RecallCancelPlugin
