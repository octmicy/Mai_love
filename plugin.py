"""麦麦恋人（MaiLover）插件入口

MaiBot 私聊专用虚拟恋人插件（NapCat 适配器）。
核心逻辑：日程定骨架 + 概率定节奏 + 情绪定温度。

v2.0.0 架构变更：
- 主动发言统一触发 planner（ctx.maisaka.proactive.trigger），
  不再自行调 LLM + send.text
- 新增 planner.before_request Hook，注入麦麦当前活动状态
- 新增 mai_lover_current_activity Tool，供 planner 查询麦麦在干嘛
- 人设缓存：on_load 时读取 ctx.config.get("personality.personality")，
  on_config_update(scope="bot") 时刷新
- 删除关键词拦截（hook_handler.py），消息正常放行给 planner

导出 create_plugin() 函数返回 MaiLoverPlugin 实例。
"""

import asyncio
import os
from datetime import datetime
from typing import Any, ClassVar, Iterable, Optional

from maibot_sdk import API, Command, HookHandler, MaiBotPlugin, Tool
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

from .affection_manager import AffectionManager
from .config import MaiLoverPluginSettings
from .constants import AFFECTION_DESCRIPTIONS
from .holiday_service import HolidayService
from .llm_service import LLMService
from .memory_manager import MemoryManager
from .message_service import MessageService
from .schedule_generator import ScheduleGenerator
from .scheduler import Scheduler


class MaiLoverPlugin(MaiBotPlugin):
    """麦麦恋人插件主类。

    组装所有模块，管理插件生命周期：
    - on_load: 初始化所有子模块，缓存人设，启动调度器
    - on_unload: 停止调度器，刷新好感度数据
    - on_config_update: 热重载配置，scope="bot" 时刷新人设
    - on_planner_before_request: 注入麦麦当前活动到 planner extra_prompt
    - Tools: mai_lover_status / mai_lover_schedule / mai_lover_send_message /
             mai_lover_affection / mai_lover_config / mai_lover_current_activity
    - Commands: /mai_status / /mai_schedule / /mai_affection / /mai_help /
                /mai_config / /mai_test
    """

    config_model = MaiLoverPluginSettings

    # 订阅主程序 bot 配置热重载（含 personality.personality）
    config_reload_subscriptions: ClassVar[Iterable[str]] = ("bot",)

    def __init__(self) -> None:
        super().__init__()
        self._affection_mgr: Optional[AffectionManager] = None
        self._memory_mgr: Optional[MemoryManager] = None
        self._llm_svc: Optional[LLMService] = None
        self._message_svc: Optional[MessageService] = None
        self._holiday_svc: Optional[HolidayService] = None
        self._schedule_gen: Optional[ScheduleGenerator] = None
        self._scheduler: Optional[Scheduler] = None
        self._cached_stream_id: str = ""
        self._cached_personality: str = ""
        self._stream_retry_task: Optional[asyncio.Task[Any]] = None
        self._cached_nickname: str = ""

    # ── Lifecycle ──────────────────────────────────────────────────────

    async def on_load(self) -> None:
        """初始化插件：创建所有子模块，缓存人设，启动调度器。"""
        data_dir = self._get_data_dir()
        self.ctx.logger.info(f"MaiLover 数据目录: {data_dir}")
        os.makedirs(data_dir, exist_ok=True)

        if not self.config.plugin.enabled:
            self.ctx.logger.info("MaiLover 插件已禁用（plugin.enabled=false），跳过初始化")
            return

        # 先读主程序配置（人格 + bot.昵称），后续子模块需要用到
        await self._refresh_bot_config()

        self._affection_mgr = AffectionManager(data_dir)
        self._affection_mgr.update_level(self.config.affection.current_level)
        self._memory_mgr = MemoryManager(self._affection_mgr)
        self._llm_svc = LLMService(self.ctx, self.config)
        lover_name = self._get_lover_name()
        self._message_svc = MessageService(
            self.ctx, self.config, self._affection_mgr, lover_name
        )
        self._holiday_svc = HolidayService(self.config)
        self._schedule_gen = ScheduleGenerator(
            data_dir, self.config, self._llm_svc, self._holiday_svc
        )

        # 创建调度器（v2.0.0: 仅 4 个依赖，不再传 message_svc/llm_svc/memory_mgr）
        self._scheduler = Scheduler(
            self.ctx, self.config, self._affection_mgr, self._schedule_gen,
        )
        self._scheduler.set_personality(self._cached_personality)
        if lover_name:
            self._scheduler.set_lover_name(lover_name)

        target_qq = str(self.config.whitelist.target_qq)
        if not target_qq or target_qq == "123456789":
            self.ctx.logger.warning("target_qq 未配置或为默认值，请修改")

        await self._start_scheduler()

        # 预热当天节假日缓存，避免首次 planner 请求阻塞在 API 调用
        if self._holiday_svc:
            today = datetime.now().strftime("%Y-%m-%d")
            asyncio.create_task(self._holiday_svc.get_holiday_info(today))

        self.ctx.logger.info("MaiLover 插件加载完成")

    async def on_unload(self) -> None:
        """卸载插件：停止调度器，刷新好感度持久化。"""
        self.ctx.logger.info("MaiLover 插件正在卸载...")
        if self._affection_mgr is not None:
            self._affection_mgr.flush()
        if self._scheduler is not None:
            self._scheduler.stop()
        if self._stream_retry_task is not None:
            self._stream_retry_task.cancel()
            try:
                await asyncio.wait_for(self._stream_retry_task, timeout=1.0)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass
            self._stream_retry_task = None
        self.ctx.logger.info("MaiLover 插件已卸载")

    async def on_config_update(
        self, scope: str, config_data: dict[str, Any], version: str
    ) -> None:
        """热重载。

        self.config 已由 MaiBot 自动更新为最新值。
        scope="bot" 时额外刷新人设缓存（主程序人格配置变更）。
        """
        self.ctx.logger.info(f"配置热更新: scope={scope}, version={version}")

        if scope == "bot":
            await self._refresh_bot_config()
            if self._scheduler is not None:
                self._scheduler.set_personality(self._cached_personality)
                self._scheduler.set_lover_name(self._get_lover_name())
            self.ctx.logger.info("主程序人设已同步，无需重启 MaiLover 调度器")
            return None

        if not self.config.plugin.enabled:
            if self._scheduler is not None:
                self._scheduler.stop()
            return None

        if self._affection_mgr is not None:
            self._affection_mgr.update_level(self.config.affection.current_level)
            self._affection_mgr.flush()

        # 同步子模块配置引用
        if self._scheduler is not None:
            self._scheduler._config = self.config
        if self._llm_svc is not None:
            self._llm_svc._config = self.config
        if self._message_svc is not None:
            self._message_svc._config = self.config
        if self._schedule_gen is not None:
            self._schedule_gen._config = self.config

        # 停止旧调度器并重启
        if self._scheduler is not None:
            self._scheduler.stop()
        await self._start_scheduler()

        self.ctx.logger.info("配置热更新完成")
        return None

    # ── HookHandler ────────────────────────────────────────────────────

    @HookHandler(
        "chat.receive.after_process",
        name="mai_lover_target_private_message_observer",
        description="记录白名单用户的私聊时间，供主动聊天冷却和想念机制使用",
        mode=HookMode.OBSERVE,
        order=HookOrder.LATE,
        timeout_ms=1000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def on_target_private_message(
        self, message: dict[str, Any] | None = None, **kwargs: Any
    ) -> None:
        """记录目标用户的入站私聊；群聊和其他用户不影响恋人调度。"""
        del kwargs
        if not self._affection_mgr or not self._is_target_private_message(message):
            return

        self._affection_mgr.update_last_user_msg_time(datetime.now())
        self.ctx.logger.debug("已更新目标用户最后私聊时间")

    def _is_target_private_message(self, message: Any) -> bool:
        if not isinstance(message, dict) or message.get("is_notify"):
            return False
        message_info = message.get("message_info")
        if not isinstance(message_info, dict) or message_info.get("group_info"):
            return False
        user_info = message_info.get("user_info")
        if not isinstance(user_info, dict):
            return False
        user_id = str(user_info.get("user_id", "")).strip()
        target_qq = str(self.config.whitelist.target_qq).strip()
        return bool(user_id and target_qq and user_id == target_qq)

    @HookHandler("maisaka.planner.before_request")
    async def on_planner_before_request(self, **kwargs: Any) -> dict[str, Any]:
        """注入麦麦当前活动状态到 planner 的 extra_prompt。

        每次 planner 请求时触发（含用户正常回复和 proactive trigger），
        让麦麦在任何时候都知道自己在做什么。

        追加方式（非覆盖）：在已有 extra_prompt 后拼接状态后缀。
        """
        if not self._schedule_gen or not self._holiday_svc:
            return {"action": "continue", "modified_kwargs": kwargs}

        # 防护：kwargs 过大时跳过注入（避免触发主程序帧大小限制）
        try:
            import json as _json
            kwargs_size = len(_json.dumps(kwargs, default=str, ensure_ascii=False))
            if kwargs_size > 1_000_000:  # 1MB
                self.ctx.logger.warning(
                    f"planner kwargs 过大 ({kwargs_size} bytes)，跳过活动注入"
                )
                return {"action": "continue", "modified_kwargs": kwargs}
        except Exception:
            pass

        now = datetime.now()
        current_date = now.strftime("%Y-%m-%d")
        weekday = "一二三四五六日"[now.weekday()]
        holiday_info = await self._holiday_svc.get_holiday_info(current_date)
        current_time = now.strftime("%H:%M")
        activity = self._schedule_gen.get_current_activity(now)
        name = self._get_lover_name()
        affection_level = self._affection_mgr.level() if self._affection_mgr else 0
        affection_desc = AFFECTION_DESCRIPTIONS.get(affection_level, "")
        suffix = (
            f"\n【当前日期】今天是 {current_date}（星期{weekday}），{holiday_info}。"
            f"\n【{name}当前状态】现在 {current_time}，{name}正在{activity}。"
            f"\n【{name}对用户的好感度】档位 {affection_level}（{affection_desc}）"
        )

        # 追加非覆盖
        kwargs["extra_prompt"] = (kwargs.get("extra_prompt") or "") + suffix
        return {"action": "continue", "modified_kwargs": kwargs}

    @HookHandler("maisaka.replyer.after_response", mode=HookMode.OBSERVE)
    async def on_replyer_after_response(self, **kwargs: Any) -> None:
        """planner 实际生成回复时，补计 0.5 触发数。

        与 _trigger_planner 的 0.5 配合：回复成功总计 1.0，未回复总计 0.5。
        仅在最近 90 秒内有 proactive trigger 时生效，避免误匹配用户消息的回复。
        """
        if not self._scheduler or not self._affection_mgr:
            return

        last_trigger = self._scheduler.get_last_trigger_time()
        if last_trigger is None:
            return  # 没有待确认的 proactive trigger

        now = datetime.now()
        # 90 秒窗口内的回复视为对 proactive trigger 的响应
        if (now - last_trigger).total_seconds() < 90:
            self._affection_mgr.increment_speak(0.5)  # 补计 0.5
            self._scheduler.clear_last_trigger_time()
            self.ctx.logger.debug("replyer 回复 detected，补计 0.5 触发数")

    # ── Tools (LLM 可主动调用) ─────────────────────────────────────────
    # 注意：stream_id 由插件内部维护，LLM 调用时无需传入。

    @Tool(
        name="mai_lover_current_activity",
        description="查询麦麦现在正在做什么。当用户问'在干嘛''在做什么'或想了解麦麦当前状态时调用。",
    )
    async def tool_mai_lover_current_activity(self, **kwargs: Any) -> str:
        """LLM Tool: 查询麦麦当前活动。"""
        if not self._schedule_gen:
            return "日程服务未初始化。"
        activity = self._schedule_gen.get_current_activity(datetime.now())
        name = self._get_lover_name()
        return f"{name}现在正在{activity}。"

    @Tool(
        name="mai_lover_status",
        description="查看麦麦恋人的当前状态：好感度档位、今日发言次数、日程摘要",
    )
    async def tool_mai_lover_status(self, **kwargs: Any) -> str:
        """LLM Tool: 查看麦麦状态。"""
        return self._build_status_report()

    @Tool(
        name="mai_lover_schedule",
        description="查看麦麦的今日完整日程安排",
    )
    async def tool_mai_lover_schedule(self, **kwargs: Any) -> str:
        """LLM Tool: 查看今日日程。"""
        return self._build_schedule_report()

    @Tool(
        name="mai_lover_send_message",
        description="以麦麦恋人的口吻向用户主动发送一条恋人消息",
        parameters={
            "message": {
                "type": "string",
                "description": "要发送的消息文本（可选，留空则自动生成）",
                "required": False,
            },
        },
    )
    async def tool_mai_lover_send_message(
        self, message: str = "", **kwargs: Any
    ) -> str:
        """LLM Tool: 主动发送恋人消息。"""
        stream_id = self._cached_stream_id
        name = self._get_lover_name()
        if not stream_id:
            return f"{name}还没有连接到目标用户，请稍后再试。"
        if not self._message_svc or not self._llm_svc:
            return "消息或 LLM 服务未初始化，无法发送。"

        if not message:
            message = await self._llm_svc.generate_or_fallback(
                prompt="请用温柔恋人的口吻说一句问候或分享一件小事（20-40字）。",
                fallback="想你了呢~在忙什么呀？",
                system_prompt=f"你是一个温柔体贴的虚拟恋人「{name}」。",
                temperature=0.8,
            )

        final_text = self._message_svc.append_affection_suffix(message)
        try:
            result = await self.ctx.send.text(text=final_text, stream_id=stream_id)
            if result:
                if self._affection_mgr:
                    self._affection_mgr.increment_speak()
                return f"消息已发送: {final_text[:60]}..."
            return f"消息发送失败: 发送返回 False"
        except Exception as e:
            self.ctx.logger.error(f"[tool_mai_lover_send_message] 发送异常: {e}")
            return f"消息发送异常: {e}"

    @Tool(
        name="mai_lover_affection",
        description="调整麦麦恋人的好感度档位。0=熟悉（温柔拘谨），1=亲密（活泼热情），2=热恋（撒娇抱抱）",
        parameters={
            "level": {
                "type": "integer",
                "description": "好感度档位，只能填 0、1 或 2",
                "required": True,
            },
        },
    )
    async def tool_mai_lover_affection(self, level: int = 0, **kwargs: Any) -> str:
        """LLM Tool: 调整好感度。"""
        if not self._affection_mgr:
            return "好感度管理器未初始化。"
        if level not in (0, 1, 2):
            return f"档位只能填 0（熟悉）、1（亲密）或 2（热恋），收到的是 {level}。"
        self._affection_mgr.update_level(level)
        descs = {0: "熟悉（温柔拘谨）", 1: "亲密（活泼热情）", 2: "热恋（撒娇抱抱）"}
        self.ctx.logger.info(f"好感度已通过 Tool 调整为 {level}")
        return f"好感度已更新: {level} - {descs[level]}"

    @Tool(
        name="mai_lover_config",
        description="查看麦麦恋人当前插件配置：巡检间隔、时间窗口、概率、今日发言上限、好感度",
    )
    async def tool_mai_lover_config(self, **kwargs: Any) -> str:
        """LLM Tool: 查看配置。"""
        s = self.config.schedule
        t = self.config.time_windows
        p = self.config.probability
        a = self.config.affection
        return (
            "⚙️ 麦麦恋人配置: "
            f"巡检间隔 {s.check_interval_minutes}min | "
            f"每日上限 {s.daily_max_speak} 条 | "
            f"冷却 {s.user_cooldown_minutes}min | "
            f"触发开关 {'开' if s.proactive_trigger_enabled else '关'} | "
            f"早安 {t.morning_start}~{t.morning_end} | "
            f"晚安 {t.night_start}~{t.night_end} | "
            f"想念触发 >{t.miss_trigger_hours}h | "
            f"日常概率 {p.default_speak_rate} | "
            f"想念概率 {p.miss_speak_rate} | "
            f"日程节点概率 {p.activity_trigger_rate} | "
            f"好感度 {a.current_level} | "
            f"模型 {self.config.plugin.llm_model}"
        )

    # ── API (供其他插件调用) ────────────────────────────────────────────

    @API(
        name="get_current_activity",
        description="获取麦麦当前正在做什么。返回活动描述字符串。",
        version="1",
        public=True,
    )
    async def api_get_current_activity(self, **kwargs: Any) -> str:
        """API: 获取麦麦当前活动。"""
        if not self._schedule_gen:
            return ""
        return self._schedule_gen.get_current_activity(datetime.now())

    @API(
        name="get_schedule",
        description="获取麦麦今日完整日程。返回节点列表 [{time, activity}]。",
        version="1",
        public=True,
    )
    async def api_get_schedule(self, **kwargs: Any) -> list[dict[str, Any]]:
        """API: 获取今日日程。"""
        if not self._schedule_gen:
            return []
        today_str = datetime.now().strftime("%Y-%m-%d")
        return self._schedule_gen.load_cached_schedule(today_str)

    @API(
        name="get_affection_level",
        description="获取当前好感度档位。返回 0/1/2。",
        version="1",
        public=True,
    )
    async def api_get_affection_level(self, **kwargs: Any) -> int:
        """API: 获取好感度档位。"""
        if not self._affection_mgr:
            return 0
        return self._affection_mgr.level()

    # ── Commands (用户手动交互) ────────────────────────────────────────

    @Command(name="/mai_status", pattern=r"^/mai_status\b", description="查看麦麦恋人状态（好感度、发言计数、日程摘要）")
    async def cmd_mai_status(self, **kwargs: Any) -> tuple[bool, str, int]:
        """查看麦麦恋人状态。主动发送报告给用户并拦截消息。"""
        stream_id = str(kwargs.get("stream_id", ""))
        report = self._build_status_report()
        try:
            await self.ctx.send.text(text=report, stream_id=stream_id)
            return True, "状态已发送", 2
        except Exception as e:
            self.ctx.logger.error(f"cmd_mai_status 发送失败: {e}")
            return False, f"发送失败: {e}", 2

    @Command(name="/mai_schedule", pattern=r"^/mai_schedule\b", description="查看麦麦今日完整日程")
    async def cmd_mai_schedule(self, **kwargs: Any) -> tuple[bool, str, int]:
        """查看麦麦今日完整日程。主动发送报告给用户并拦截消息。"""
        stream_id = str(kwargs.get("stream_id", ""))
        report = self._build_schedule_report()
        try:
            await self.ctx.send.text(text=report, stream_id=stream_id)
            return True, "日程已发送", 2
        except Exception as e:
            self.ctx.logger.error(f"cmd_mai_schedule 发送失败: {e}")
            return False, f"发送失败: {e}", 2

    @Command(name="/mai_affection", pattern=r"^/mai_affection\b", description="调整好感度档位。用法: /mai_affection <0|1|2>")
    async def cmd_mai_affection(self, **kwargs: Any) -> tuple[bool, str, int]:
        """调整好感度档位。解析参数、发送确认/用法消息给用户并拦截消息。"""
        stream_id = str(kwargs.get("stream_id", ""))
        raw_message = str(kwargs.get("text", "")).strip()
        parts = raw_message.split()
        if len(parts) < 2:
            current = self._affection_mgr.level() if self._affection_mgr else "?"
            usage = (
                f"用法: /mai_affection <0|1|2>\n"
                f"0 = 熟悉（温柔拘谨）\n"
                f"1 = 亲密（活泼热情）\n"
                f"2 = 热恋（撒娇抱抱）\n"
                f"当前档位: {current}"
            )
            try:
                await self.ctx.send.text(text=usage, stream_id=stream_id)
            except Exception as e:
                self.ctx.logger.error(f"cmd_mai_affection 用法发送失败: {e}")
            return False, "参数错误", 2
        level_str = parts[1]
        try:
            level = int(level_str)
        except (ValueError, TypeError):
            msg = f"「{level_str}」不是有效数字，请使用 0、1 或 2。"
            try:
                await self.ctx.send.text(text=msg, stream_id=stream_id)
            except Exception as e:
                self.ctx.logger.error(f"cmd_mai_affection 错误提示发送失败: {e}")
            return False, "参数错误", 2
        if level not in (0, 1, 2):
            msg = f"好感度档位只能是 0（熟悉）、1（亲密）或 2（热恋），收到的是 {level}。"
            try:
                await self.ctx.send.text(text=msg, stream_id=stream_id)
            except Exception as e:
                self.ctx.logger.error(f"cmd_mai_affection 错误提示发送失败: {e}")
            return False, "参数错误", 2
        if not self._affection_mgr:
            msg = "好感度管理器未初始化，无法调整。"
            try:
                await self.ctx.send.text(text=msg, stream_id=stream_id)
            except Exception as e:
                self.ctx.logger.error(f"cmd_mai_affection 错误提示发送失败: {e}")
            return False, "管理器未初始化", 2
        self._affection_mgr.update_level(level)
        descs = {0: "熟悉（温柔拘谨）", 1: "亲密（活泼热情）", 2: "热恋（撒娇抱抱）"}
        self.ctx.logger.info(f"好感度已通过命令调整为 {level}")
        confirm = f"好感度已更新: {level} - {descs.get(level, '未知')}"
        try:
            await self.ctx.send.text(text=confirm, stream_id=stream_id)
            return True, "好感度已调整", 2
        except Exception as e:
            self.ctx.logger.error(f"cmd_mai_affection 确认发送失败: {e}")
            return False, f"发送失败: {e}", 2

    @Command(name="/mai_help", pattern=r"^/mai_help\b", description="查看麦麦恋人所有可用命令")
    async def cmd_mai_help(self, **kwargs: Any) -> tuple[bool, str, int]:
        """查看所有可用命令。主动发送帮助文本给用户并拦截消息。"""
        stream_id = str(kwargs.get("stream_id", ""))
        name = self._get_lover_name()
        help_text = (
            "🐱 麦麦恋人 可用命令:\n"
            f"/mai_status    — 查看{name}状态（好感度/今日发言/日程摘要）\n"
            "/mai_schedule  — 查看今日完整日程\n"
            "/mai_affection — 调整好感度档位: /mai_affection <0|1|2>\n"
            "/mai_config    — 查看当前插件配置摘要\n"
            "/mai_test      — 发送一条测试消息（验证发送通道）"
        )
        try:
            await self.ctx.send.text(text=help_text, stream_id=stream_id)
            return True, "帮助已发送", 2
        except Exception as e:
            self.ctx.logger.error(f"cmd_mai_help 发送失败: {e}")
            return False, f"发送失败: {e}", 2

    @Command(name="/mai_config", pattern=r"^/mai_config\b", description="查看麦麦恋人当前配置摘要")
    async def cmd_mai_config(self, **kwargs: Any) -> tuple[bool, str, int]:
        """查看当前配置摘要。主动发送配置信息给用户并拦截消息。"""
        stream_id = str(kwargs.get("stream_id", ""))
        s = self.config.schedule
        t = self.config.time_windows
        p = self.config.probability
        a = self.config.affection
        summary = (
            "⚙️ 麦麦恋人配置摘要\n"
            f"调度: 巡检间隔 {s.check_interval_minutes}min | "
            f"每日上限 {s.daily_max_speak} 条 | "
            f"冷却 {s.user_cooldown_minutes}min | "
            f"触发开关 {'开启' if s.proactive_trigger_enabled else '关闭'}\n"
            f"时间窗口: 早安 {t.morning_start}~{t.morning_end} | "
            f"晚安 {t.night_start}~{t.night_end} | "
            f"想念触发 >{t.miss_trigger_hours}h\n"
            f"概率: 日常巡检 {p.default_speak_rate} | "
            f"想念 {p.miss_speak_rate} | "
            f"日程节点 {p.activity_trigger_rate}\n"
            f"好感度: {a.current_level} | "
            f"模型: {self.config.plugin.llm_model}"
        )
        try:
            await self.ctx.send.text(text=summary, stream_id=stream_id)
            return True, "配置已发送", 2
        except Exception as e:
            self.ctx.logger.error(f"cmd_mai_config 发送失败: {e}")
            return False, f"发送失败: {e}", 2

    @Command(name="/mai_test", pattern=r"^/mai_test\b", description="发送一条测试消息以验证发送通道")
    async def cmd_mai_test(self, **kwargs: Any) -> tuple[bool, str, int]:
        """发送测试消息验证发送通道。优先使用 Command 传入的 stream_id。"""
        stream_id = str(kwargs.get("stream_id", self._cached_stream_id))
        if not stream_id:
            return False, "暂无 stream_id", 2
        try:
            result = await self.ctx.send.text(
                text=f"{self._get_lover_name()}测试消息~ 发送通道正常 ✅",
                stream_id=stream_id,
            )
            if result:
                return True, "测试消息发送成功", 2
            return False, "发送返回 False", 2
        except Exception as e:
            return False, f"发送异常: {e}", 2

    # ── Status / Schedule Reports ──────────────────────────────────────

    def _build_status_report(self) -> str:
        """构造恋人状态报告文本。"""
        if not self._affection_mgr:
            return "麦麦恋人插件尚未初始化完成。"

        level_names: dict[int, str] = {0: "熟悉", 1: "亲密", 2: "热恋"}
        level = self._affection_mgr.level()
        speak_count = self._affection_mgr.today_speak_count()
        daily_max = self.config.schedule.daily_max_speak
        morning_ok = self._affection_mgr.morning_sent_today()
        night_ok = self._affection_mgr.night_sent_today()
        miss_ok = self._affection_mgr.miss_sent_today()
        today_str = datetime.now().strftime("%Y-%m-%d")

        lines: list[str] = [
            f"❤️ 麦麦恋人状态 ({today_str})",
            f"好感度档位: {level} - {level_names.get(level, '未知')}",
            f"今日触发: {speak_count}/{daily_max}",
            f"早安: {'✅已触发' if morning_ok else '❌未触发'}  |  "
            f"晚安: {'✅已触发' if night_ok else '❌未触发'}  |  "
            f"想念: {'✅已触发' if miss_ok else '❌未触发'}",
        ]

        # 日程摘要
        if self._schedule_gen:
            schedule = self._schedule_gen.load_cached_schedule(today_str)
            if schedule:
                lines.append(f"今日日程 ({len(schedule)} 个节点):")
                for node in schedule[:5]:
                    t = node.get("time", "??:??")
                    activity = node.get("activity", "未知")
                    lines.append(f"  [{t}] 🐱 {activity}")
                if len(schedule) > 5:
                    lines.append(f"  ... 还有 {len(schedule) - 5} 个节点")
            else:
                lines.append("今日暂无日程缓存。")

        return "\n".join(lines)

    def _build_schedule_report(self) -> str:
        """构造今日完整日程报告文本。"""
        today_str = datetime.now().strftime("%Y-%m-%d")

        if not self._schedule_gen:
            return "日程生成器未初始化。"

        schedule = self._schedule_gen.load_cached_schedule(today_str)
        if not schedule:
            return f"📅 今日 ({today_str}) 暂无日程缓存。\n可能尚未生成，请等待下次凌晨 {self.config.schedule.generate_hour}:00 自动生成。"

        lines: list[str] = [f"📅 {self._get_lover_name()}今日日程 ({today_str})", ""]
        for node in schedule:
            t = node.get("time", "??:??")
            activity = node.get("activity", "未知")
            lines.append(f"  [{t}] 🐱 {activity}")

        return "\n".join(lines)

    # ── Internal Helpers ───────────────────────────────────────────────

    async def _refresh_bot_config(self) -> None:
        """从主程序配置刷新人设和 bot 昵称缓存。

        读取 ctx.config.get("personality.personality") 和
        ctx.config.get("bot.nickname")，同步给 scheduler（若已创建）。
        """
        try:
            self._cached_personality = await self.ctx.config.get(
                "personality.personality", ""
            )
        except Exception as e:
            self.ctx.logger.warning(f"读取人设配置失败: {e}")
            self._cached_personality = ""

        try:
            self._cached_nickname = await self.ctx.config.get(
                "bot.nickname", ""
            )
        except Exception as e:
            self.ctx.logger.warning(f"读取 bot.nickname 失败: {e}")
            self._cached_nickname = ""

        if self._scheduler is not None:
            self._scheduler.set_personality(self._cached_personality)
        self.ctx.logger.debug(
            f"Bot 配置已刷新: nickname={self._cached_nickname}, "
            f"personality={self._cached_personality[:50]}..."
        )

    def _get_lover_name(self) -> str:
        """获取恋人名称（三级回退）。

        优先级：
        1. 插件配置 plugin.lover_name（非空时直接使用）
        2. 主程序 bot.nickname（配置留空时回退）
        3. "麦麦"（仍为空时的最终默认值）
        """
        try:
            configured = self.config.plugin.lover_name
            if configured and configured.strip():
                return configured.strip()
        except Exception:
            pass
        if self._cached_nickname:
            return self._cached_nickname
        return "麦麦"

    def _check_target_stream(self, stream_id: str) -> bool:
        """校验 stream_id 是否属于白名单目标用户。

        Args:
            stream_id: 待校验的聊天流 ID。

        Returns:
            True 表示是目标用户。
        """
        if not stream_id:
            return False
        return stream_id == self._cached_stream_id

    def _get_data_dir(self) -> str:
        """获取插件数据目录路径。

        数据写入持久化目录下，避免插件更新（git pull）时
        覆盖用户运行时数据（affection_memory.json、schedule_cache.json）。

        Returns:
            返回MaiBot框架统一插件持久化目录 ctx.path.data
        """
        return self.ctx.paths.data_dir

    async def _resolve_stream_id(self, target_qq: str) -> str:
        """获取目标 QQ 的私聊 stream_id（纯 SDK 路径）。

        使用 SDK 提供的 chat 代理，不直接访问 MaiBot 内部数据库。
        所有层级均做 try/except 降级保护。

        Args:
            target_qq: 目标 QQ 号。

        Returns:
            stream_id 字符串，获取失败返回空字符串。
        """
        # 方法1: get_stream_by_user_id（自动匹配已注册的适配器）
        try:
            stream_info = await self.ctx.chat.get_stream_by_user_id(
                user_id=target_qq
            )
            if isinstance(stream_info, dict) and stream_info.get("stream_id"):
                sid = str(stream_info["stream_id"])
                self.ctx.logger.debug(f"从 get_stream_by_user_id 获取 stream_id: {sid}")
                return sid
        except Exception as e:
            self.ctx.logger.warning(f"get_stream_by_user_id 失败: {e}")

        # 方法2: 遍历 get_private_streams
        try:
            streams = await self.ctx.chat.get_private_streams()
            if isinstance(streams, list):
                for s in streams:
                    if isinstance(s, dict) and str(s.get("user_id", "")) == str(target_qq):
                        sid = str(s.get("stream_id", ""))
                        self.ctx.logger.debug(f"从 get_private_streams(list) 获取 stream_id: {sid}")
                        return sid
            elif isinstance(streams, dict):
                for key, s in streams.items():
                    if isinstance(s, dict) and str(s.get("user_id", "")) == str(target_qq):
                        sid = str(s.get("stream_id", key))
                        self.ctx.logger.debug(f"从 get_private_streams(dict) 获取 stream_id: {sid}")
                        return sid
        except Exception as e:
            self.ctx.logger.error(f"get_private_streams 失败: {e}")

        return ""

    async def _start_scheduler(self) -> None:
        """解析 stream_id 并启动调度器。

        stream_id 解析失败时启动后台重试协程。
        日程生成循环不依赖 stream_id，始终启动。
        """
        if self._scheduler is None:
            self.ctx.logger.error("调度器未初始化，无法启动")
            return

        if self._stream_retry_task is not None:
            self._stream_retry_task.cancel()
            try:
                await asyncio.wait_for(self._stream_retry_task, timeout=1.0)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass
            self._stream_retry_task = None

        target_qq = str(self.config.whitelist.target_qq)
        if not target_qq or target_qq == "123456789":
            self.ctx.logger.warning("target_qq 未配置，调度器未启动")
            return

        # 先启动 scheduler（日程生成循环不依赖 stream_id）
        self._scheduler.set_target(target_qq, "")  # stream_id 暂空
        await self._scheduler.start()

        # 解析 stream_id
        stream_id = await self._resolve_stream_id(target_qq)
        if stream_id:
            self._cached_stream_id = stream_id
            self._scheduler.set_target(target_qq, stream_id)
            await self._scheduler.start_patrol()
            self.ctx.logger.info(f"目标用户: {target_qq}, stream_id: {stream_id}")
        else:
            self.ctx.logger.warning(
                f"无法获取 stream_id，日程生成已启动但巡检暂不可用。"
                f"将在后台快速重试解析..."
            )
            self._stream_retry_task = asyncio.create_task(
                self._retry_stream_id(target_qq)
            )

    async def _retry_stream_id(self, target_qq: str) -> None:
        """后台重试解析 stream_id。

        启动阶段使用短退避重试，避免每次重启后固定失效 5 分钟。
        达到 MAX_ATTEMPTS 次后停止，避免无限循环。
        """
        MAX_ATTEMPTS = 60  # 约 1 小时（退避到 60 秒后）
        retry_delays = (5, 10, 20, 30, 60)
        attempt = 0
        while True:
            try:
                delay = retry_delays[min(attempt, len(retry_delays) - 1)]
                await asyncio.sleep(delay)
                stream_id = await self._resolve_stream_id(target_qq)
                if stream_id:
                    self._cached_stream_id = stream_id
                    if self._scheduler is not None:
                        self._scheduler.set_target(target_qq, stream_id)
                        await self._scheduler.start_patrol()
                    self.ctx.logger.info(f"stream_id 重试成功: {stream_id}，巡检循环已就绪")
                    self._stream_retry_task = None
                    return
                else:
                    attempt += 1
                    if attempt >= MAX_ATTEMPTS:
                        self.ctx.logger.warning(
                            f"stream_id 重试已达 {attempt} 次，停止重试。请检查适配器是否正常连接。"
                        )
                        self._stream_retry_task = None
                        return
                    if attempt % 10 == 0:
                        self.ctx.logger.warning(
                            f"stream_id 已重试 {attempt} 次仍未成功"
                        )
                    self.ctx.logger.debug(
                        f"stream_id 重试失败，{retry_delays[min(attempt, len(retry_delays) - 1)]} 秒后再次重试"
                    )
            except asyncio.CancelledError:
                return
            except Exception as e:
                self.ctx.logger.error(f"stream_id 重试异常: {e}")


def create_plugin() -> MaiBotPlugin:
    """MaiBot 插件工厂函数。"""
    return MaiLoverPlugin()
