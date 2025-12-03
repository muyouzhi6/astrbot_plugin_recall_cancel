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
        
        # 存储最近撤回的消息：message_id -> {timestamp, user_id, group_id, is_private}
        # 用于处理 "先撤回，后触发LLM请求" 的竞态条件，并支持模糊匹配
        self.recalled_messages: Dict[str, Dict[str, Any]] = {}

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
            current_time = asyncio.get_running_loop().time()
            
            # 1. 精确匹配检查：该消息是否已经被撤回
            if message_id in self.recalled_messages:
                logger.info(f"LLM请求已被提前撤回(精确): {message_id}")
                event.stop_event()
                return
            
            # 2. 模糊匹配检查：检查同一发送者、同一会话、短时间内的撤回
            # 这可以处理消息ID变化的情况，以及消息ID获取不一致的问题
            # 统一转换为字符串进行比较
            sender_id = str(event.get_sender_id())
            group_id = str(event.get_group_id()) if not event.is_private_chat() else ""
            is_private = event.is_private_chat()
            
            # 将字典转换为列表以避免在迭代期间修改字典导致的RuntimeError
            for recalled_id, info in list(self.recalled_messages.items()):
                # 时间窗口检查 (10秒内，考虑到网络延迟和处理时间)
                if current_time - info["timestamp"] > 10:
                    continue
                
                # 检查发送者 (统一转str)
                if str(info["user_id"]) != sender_id:
                    continue
                
                # 检查会话环境
                is_match = False
                if is_private and info["is_private"]:
                    # 私聊匹配
                    is_match = True
                elif not is_private and not info["is_private"] and str(info["group_id"]) == group_id:
                    # 群聊匹配
                    is_match = True
                
                if is_match:
                    logger.info(f"LLM请求已被提前撤回(模糊匹配): 请求ID {message_id} -> 撤回ID {recalled_id}")
                    event.stop_event()
                    return

            self.pending_llm_requests[message_id] = {
                "session_id": event.unified_msg_origin,
                "event": event,
                "timestamp": current_time,
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

            # 检查是否是群消息撤回或好友消息撤回事件
            if post_type == "notice" and notice_type in [
                "group_recall",
                "friend_recall",
            ]:
                # 兼容 message_id 为空的情况，尝试使用 operator_id (部分协议实现可能不同)
                # 但主要还是依赖 message_id
                if not message_id:
                    logger.warning(f"撤回事件中的message_id无效: {raw_message}，尝试继续处理以支持模糊匹配")
                    # 生成一个临时ID用于模糊匹配记录，避免空键
                    import uuid
                    recalled_message_id = f"unknown_{uuid.uuid4()}"
                else:
                    recalled_message_id = str(message_id)

                logger.info(f"检测到消息撤回: {recalled_message_id} (类型: {notice_type})")
                
                # 提取上下文信息
                group_id = str(get_value(raw_message, "group_id", ""))
                user_id = str(get_value(raw_message, "user_id", "")) # 消息发送者
                if not user_id:
                     # 如果没有user_id (如operator_id)，尝试获取 operator_id
                     user_id = str(get_value(raw_message, "operator_id", ""))

                is_private = notice_type == "friend_recall"
                
                # 记录撤回的消息，防止后续可能的LLM请求 (处理竞态条件)
                self.recalled_messages[recalled_message_id] = {
                    "timestamp": asyncio.get_running_loop().time(),
                    "user_id": user_id,
                    "group_id": group_id,
                    "is_private": is_private
                }

                # 精确匹配
                matched_request = None
                match_type = "精确匹配"

                if recalled_message_id in self.pending_llm_requests:
                    matched_request = self.pending_llm_requests[recalled_message_id]
                else:
                    # 模糊匹配策略
                    # 当精确匹配失败时，尝试查找同一会话中最近的请求
                    # 条件：
                    # 1. 同一会话 (Group ID 或 User ID 匹配)
                    # 2. 同一发送者 (User ID 匹配)
                    # 3. 时间在最近 60 秒内
                    
                    current_time = asyncio.get_running_loop().time()
                    logger.debug(f"尝试模糊匹配撤回消息 {recalled_message_id}。当前 Pending 列表: {list(self.pending_llm_requests.keys())}")
                    
                    for pending_id, info in self.pending_llm_requests.items():
                        # 忽略已取消的
                        if info.get("cancelled", False):
                            continue
                            
                        pending_event = info.get("event")
                        if not pending_event:
                            continue
                            
                        # 检查时间窗口 (60秒内)
                        if current_time - info["timestamp"] > 60:
                            continue

                        # 检查发送者是否一致
                        if str(pending_event.get_sender_id()) != user_id:
                            continue
                            
                        # 检查会话是否一致
                        session_match = False
                        if notice_type == "group_recall":
                            # 群撤回：检查群号
                            if str(pending_event.get_group_id()) == group_id:
                                session_match = True
                        elif notice_type == "friend_recall":
                            # 私聊撤回：检查是否为私聊且对方ID一致
                            if pending_event.is_private_chat() and str(pending_event.get_sender_id()) == user_id:
                                session_match = True
                        
                        if session_match:
                            matched_request = info
                            match_type = f"模糊匹配 (Pending ID: {pending_id})"
                            logger.info(f"模糊匹配成功: 撤回ID {recalled_message_id} -> 请求ID {pending_id}")
                            break

                if matched_request:
                    matched_request["cancelled"] = True

                    # 尝试停止相关事件
                    if "event" in matched_request:
                        matched_request["event"].stop_event()

                    logger.info(f"已取消对应的LLM回复: {recalled_message_id} [{match_type}]")
                else:
                    logger.debug(f"撤回的消息 {recalled_message_id} 没有找到对应的LLM请求 (精确或模糊)")

                # 阻止此撤回事件继续传播
                event.stop_event()
        except Exception as e:
            # 记录异常信息以便调试，但不阻断处理流程
            logger.error(f"处理撤回事件时出现异常: {e}", exc_info=True)
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
                    self.pending_llm_requests.pop(msg_id, None)
                    logger.debug(f"清理过期LLM请求记录: {msg_id}")
                
                # 清理超过5分钟的撤回消息记录
                expired_recalls = []
                for msg_id, info in list(self.recalled_messages.items()):
                    if current_time - info["timestamp"] > 300: # 5分钟
                        expired_recalls.append(msg_id)
                
                for msg_id in expired_recalls:
                    self.recalled_messages.pop(msg_id, None)
                    # logger.debug(f"清理过期撤回记录: {msg_id}") # 过于频繁，注释掉

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
        self.recalled_messages.clear()
        logger.info("RecallCancelPlugin 已卸载")


# 为了向后兼容，保留Main类
Main = RecallCancelPlugin
