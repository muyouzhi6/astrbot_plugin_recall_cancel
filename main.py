import asyncio
from typing import Dict, Any
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register


@register(
    "astrbot_plugin_recall_cancel",
    "木有知",
    "撤了就当没发，当用户撤回触发LLM回应的消息时，如果LLM回复还未发送则取消发送。防止用户发错消息撤回后机器人仍然回复的情况，提升用户体验并避免资源浪费。",
    "v1.0.0",
)
class RecallCancelPlugin(Star):
    """消息撤回取消插件

    当用户撤回触发LLM回应的消息时，如果LLM的回复还没发送出去，就取消发送。
    这能防止用户发错了消息撤回了但是astrbot还傻乎乎的回复，或者有人恶意发了一大串消息后撤回的情况。
    """

    def __init__(self, context: Context):
        super().__init__(context)

        # 存储正在处理的LLM请求：message_id -> session_info
        self.pending_llm_requests: Dict[str, Dict[str, Any]] = {}

        # 清理任务
        self.cleanup_task = None

        logger.info("RecallCancelPlugin 已加载")

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self):
        """AstrBot加载完成后启动清理任务"""
        self.cleanup_task = asyncio.create_task(self._cleanup_expired_records())

    @filter.on_llm_request(priority=1)
    async def track_llm_request(self, event: AstrMessageEvent, req):
        """跟踪LLM请求开始"""
        if not event.message_obj.message_id:
            return
            
        message_id = str(event.message_obj.message_id)
        if message_id:
            self.pending_llm_requests[message_id] = {
                "session_id": event.unified_msg_origin,
                "event": event,
                "timestamp": asyncio.get_running_loop().time(),
                "cancelled": False,
            }
            logger.debug(f"记录LLM请求: {message_id} - {event.unified_msg_origin}")

    @filter.on_llm_response(priority=1)
    async def track_llm_response(self, event: AstrMessageEvent, resp):
        """跟踪LLM响应完成"""
        if not event.message_obj.message_id:
            return
            
        message_id = str(event.message_obj.message_id)
        if message_id in self.pending_llm_requests:
            # 检查是否已被撤回信息
            if self.pending_llm_requests[message_id].get("cancelled", False):
                logger.info(f"LLM响应已被撤回取消: {message_id}")
                event.stop_event()  # 阻止后续发送
                # 清理已取消的请求记录
                self.pending_llm_requests.pop(message_id, None)
                return

            # 不要在这里删除记录，因为消息还未发送
            # 记录的清理应该在消息真正发送后进行
            logger.debug(f"LLM响应已生成，等待发送: {message_id}")

    @filter.on_decorating_result(priority=1)
    async def check_before_send(self, event: AstrMessageEvent):
        """在消息发送前最后检查是否已被撤回"""
        if not event.message_obj.message_id:
            return
            
        message_id = str(event.message_obj.message_id)
        
        # 如果不在记录中，直接返回
        if message_id not in self.pending_llm_requests:
            return
            
        # 第一次检查
        if self.pending_llm_requests[message_id].get("cancelled", False):
            logger.info(f"发送前检测到撤回取消: {message_id}")
            event.stop_event()  # 阻止发送
            self.pending_llm_requests.pop(message_id, None)
            return

        # 增加微小延迟，处理秒撤回的竞态条件
        # 这里必须加上 try-except，防止在 sleep 期间 key 被删除导致 KeyError
        try:
            await asyncio.sleep(0.5)
            
            # 再次检查 key 是否存在 (可能已被清理)
            if message_id not in self.pending_llm_requests:
                return

            # 第二次检查
            if self.pending_llm_requests[message_id].get("cancelled", False):
                logger.info(f"发送前(延迟后)检测到撤回取消: {message_id}")
                event.stop_event()  # 阻止发送
                self.pending_llm_requests.pop(message_id, None)
                return
        except Exception as e:
            logger.warning(f"撤回检查过程出现异常: {e}")

    @filter.after_message_sent(priority=1)
    async def clean_sent_message(self, event: AstrMessageEvent):
        """消息发送后清理记录"""
        if not event.message_obj.message_id:
            return
            
        message_id = str(event.message_obj.message_id)
        if message_id in self.pending_llm_requests:
            self.pending_llm_requests.pop(message_id, None)
            logger.debug(f"清理已发送消息的记录: {message_id}")

    @filter.command("recall_status", alias={"撤回状态"})
    async def show_status(self, event: AstrMessageEvent):
        """显示插件状态 - 用于调试"""
        pending_count = len(self.pending_llm_requests)

        status_msg = "📊 撤回取消插件状态:\n"
        status_msg += f"🔄 待处理LLM请求: {pending_count}\n"
        status_msg += f"🔧 清理任务: {'运行中' if self.cleanup_task and not self.cleanup_task.done() else '已停止'}"

        if pending_count > 0:
            status_msg += "\n\n📝 当前待处理请求:"
            for msg_id in list(self.pending_llm_requests.keys())[:5]:  # 最多显示5个
                status_msg += f"\n- {msg_id}"
            if pending_count > 5:
                status_msg += f"\n- ... 还有 {pending_count - 5} 个"

        yield event.plain_result(status_msg)

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.ALL, priority=1)
    async def handle_recall_event(self, event: AstrMessageEvent):
        """处理消息撤回事件（OneBot V11标准）"""
        raw_message = event.message_obj.raw_message

        try:
            # 统一处理不同格式的 raw_message，兼容字典和对象属性访问
            def get_value(obj, key, default=None):
                """统一获取值的方法，兼容字典和对象属性"""
                try:
                    if hasattr(obj, "__getitem__"):
                        return obj[key]  # type: ignore
                except (KeyError, TypeError):
                    pass
                return getattr(obj, key, default)

            post_type = get_value(raw_message, "post_type")
            notice_type = get_value(raw_message, "notice_type")
            message_id = get_value(raw_message, "message_id")

            logger.debug(
                f"检测到事件: post_type={post_type}, notice_type={notice_type}, message_id={message_id}"
            )

            # 检查是否是群消息撤回或好友消息撤回事件
            if post_type == "notice" and notice_type in [
                "group_recall",
                "friend_recall",
            ]:
                # 直接检查 message_id 是否有效
                if not message_id:
                    logger.debug("撤回事件中的message_id无效，忽略")
                    return

                recalled_message_id = str(message_id)
                logger.info(f"检测到消息撤回: {recalled_message_id}")

                # 检查是否有对应的LLM请求正在处理
                if recalled_message_id in self.pending_llm_requests:
                    request_info = self.pending_llm_requests[recalled_message_id]
                    request_info["cancelled"] = True

                    # 尝试停止相关事件
                    if "event" in request_info:
                        request_info["event"].stop_event()

                    logger.info(f"已取消对应的LLM回复: {recalled_message_id}")
                else:
                    logger.debug(f"撤回的消息 {recalled_message_id} 没有对应的LLM请求")

                # 阻止此撤回事件继续传播
                event.stop_event()
        except Exception as e:
            # 记录异常信息以便调试，但不阻断处理流程
            logger.debug(f"处理撤回事件时出现异常: {e}")
            pass

    async def _cleanup_expired_records(self):
        """定期清理过期的记录"""
        while True:
            try:
                await asyncio.sleep(300)  # 每5分钟清理一次
                current_time = asyncio.get_running_loop().time()

                # 清理超过10分钟的LLM请求记录
                expired_requests = []
                for msg_id, info in list(self.pending_llm_requests.items()):
                    if current_time - info["timestamp"] > 600:  # 10分钟
                        expired_requests.append(msg_id)

                for msg_id in expired_requests:
                    del self.pending_llm_requests[msg_id]
                    logger.debug(f"清理过期LLM请求记录: {msg_id}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"清理任务异常: {e}")

    async def terminate(self):
        """插件卸载时的清理工作"""
        if self.cleanup_task and not self.cleanup_task.done():
            self.cleanup_task.cancel()
            try:
                await self.cleanup_task
            except asyncio.CancelledError:
                pass

        self.pending_llm_requests.clear()
        logger.info("RecallCancelPlugin 已卸载")


# 为了向后兼容，保留Main类
Main = RecallCancelPlugin
