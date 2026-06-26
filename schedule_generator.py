"""日程生成模块

负责每日日程的生成与缓存：
1. 调用节假日 API 获取日期信息
2. 读取 mai_template.json（麦麦作息骨架）
3. 构造 Prompt（含人设性格）→ LLM 生成日程
4. LLM 失败 → 使用原始模板骨架
5. 存入 schedule_cache.json

v2.0.0 变更：
- 模板文件从 user_template.json 改为 mai_template.json
- generate_daily_schedule 新增 personality 参数
- 新增 get_current_activity 公共方法供 Hook/Tool 查询当前活动
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .config import MaiLoverPluginSettings
from .holiday_service import HolidayService
from .llm_service import LLMService


class ScheduleGenerator:
    """日程生成器。

    每日凌晨运行，调用 LLM 生成当天的日程节点列表，
    同时缓存到 schedule_cache.json 供调度器使用。

    v2.0.0: 日程节点格式为 {time, activity}，描述麦麦的虚拟日常活动。
    """

    def __init__(
        self,
        data_dir: str,
        config: MaiLoverPluginSettings,
        llm_service: LLMService,
        holiday_service: HolidayService,
    ) -> None:
        """初始化日程生成器。

        Args:
            data_dir: 数据目录路径（用于缓存文件）。
            config: 插件强类型配置模型（预留给未来功能）。
            llm_service: LLM 服务。
            holiday_service: 节假日服务。
        """
        self._cache_file: Path = Path(data_dir) / "schedule_cache.json"
        # 模板文件在插件根目录（data_dir 的上一级），不是 data/ 子目录
        self._template_file: Path = Path(data_dir).parent / "mai_template.json"
        self._config: MaiLoverPluginSettings = config
        self._llm: LLMService = llm_service
        self._holiday: HolidayService = holiday_service

    async def generate_daily_schedule(
        self, date: str, personality: str = ""
    ) -> list[dict[str, Any]]:
        """生成当日日程。

        流程：
        1. 调用节假日 API
        2. 读取 mai_template.json
        3. 构造 Prompt（含人设性格）→ LLM 生成
        4. LLM 失败 → 使用 mai_template.json 原始骨架
        5. 存入 schedule_cache.json

        Args:
            date: 日期字符串（YYYY-MM-DD）。
            personality: 麦麦人设性格文本（从 ctx.config.get 读取）。

        Returns:
            日程节点列表 [{time, activity}, ...]。
        """
        # 1. 获取节假日信息
        holiday_info = await self._holiday.get_holiday_info(date)

        # 2. 读取麦麦作息模板
        template_text = self._read_template()

        # 3. 调用 LLM 生成日程（传入人设性格）
        nodes: list[dict[str, Any]] = []
        try:
            nodes = await self._llm.generate_schedule(
                date, holiday_info, template_text, personality
            )
        except Exception:
            # LLM 生成异常，将在下一步使用降级骨架
            pass

        # 4. LLM 失败 → 使用原始模板骨架
        if not nodes:
            nodes = self._build_fallback_schedule(date)

        # 5. 缓存到文件
        self._save_cache(date, nodes)

        return nodes

    def load_cached_schedule(self, date: str) -> list[dict[str, Any]]:
        """读取缓存的日程。

        日期不匹配则返回空列表。

        Args:
            date: 日期字符串（YYYY-MM-DD）。

        Returns:
            日程节点列表，或空列表。
        """
        if not self._cache_file.exists():
            return []
        try:
            with open(self._cache_file, "r", encoding="utf-8") as f:
                cache = json.load(f)
            if cache.get("date") == date and cache.get("nodes"):
                return cache["nodes"]
        except (json.JSONDecodeError, IOError):
            pass
        return []

    def get_current_activity(self, now: datetime) -> str:
        """查找当前时间点麦麦正在做的活动。

        查找逻辑：
        1. 加载今日日程缓存
        2. 在所有 time <= 当前时间的节点中，取最后一个的 activity
        3. 无日程或无匹配节点 → 返回 "今天还没有安排"

        兼容旧格式节点：若无 activity 字段则跳过该节点。

        Args:
            now: 当前时间。

        Returns:
            活动描述字符串。
        """
        today_str = now.strftime("%Y-%m-%d")
        schedule = self.load_cached_schedule(today_str)
        if not schedule:
            return "今天还没有安排"

        now_minutes = now.hour * 60 + now.minute
        current_activity: Optional[str] = None

        for node in schedule:
            node_time = str(node.get("time", ""))
            if not node_time:
                continue
            node_minutes = self._time_to_minutes(node_time)
            if node_minutes is None:
                continue
            # 节点时间 <= 当前时间 → 麦麦可能正在做这件事
            if node_minutes <= now_minutes:
                activity = str(node.get("activity", ""))
                if activity:
                    current_activity = activity
            else:
                # 节点时间 > 当前时间 → 后续节点还没开始，停止遍历
                break

        if current_activity:
            return current_activity
        return "今天还没有安排"

    def _read_template(self) -> str:
        """读取 mai_template.json 并返回格式化文本。

        Returns:
            格式化后的模板文本。
        """
        if not self._template_file.exists():
            return "{}"
        try:
            with open(self._template_file, "r", encoding="utf-8") as f:
                return f.read()
        except IOError:
            return "{}"

    def _save_cache(self, date: str, nodes: list[dict[str, Any]]) -> None:
        """保存日程到缓存文件。

        Args:
            date: 日期字符串。
            nodes: 日程节点列表。
        """
        try:
            self._cache_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._cache_file, "w", encoding="utf-8") as f:
                json.dump(
                    {"date": date, "nodes": nodes}, f, ensure_ascii=False, indent=2
                )
        except IOError:
            pass

    def _build_fallback_schedule(self, date: str) -> list[dict[str, Any]]:
        """使用原始模板构建降级日程。

        根据日期是工作日还是周末选择合适的模板。
        mai_template.json 的节点已是 {time, activity} 格式，无需额外处理。

        Args:
            date: 日期字符串。

        Returns:
            日程节点列表 [{time, activity}, ...]。
        """
        try:
            dt = datetime.strptime(date, "%Y-%m-%d")
            is_weekend = dt.weekday() >= 5
        except ValueError:
            is_weekend = False

        template = self._load_template_json()
        if is_weekend and "weekend" in template:
            return template["weekend"]
        if "workday" in template:
            return template["workday"]
        return []

    def _load_template_json(self) -> dict[str, Any]:
        """加载 mai_template.json 为字典。

        Returns:
            模板字典，含 'workday' 和 'weekend' 键。
        """
        if not self._template_file.exists():
            return {}
        try:
            with open(self._template_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}

    @staticmethod
    def _time_to_minutes(time_str: str) -> Optional[int]:
        """将 HH:MM 时间字符串转换为当天分钟数。

        Args:
            time_str: 时间字符串（如 "08:30"）。

        Returns:
            分钟数（如 510），解析失败返回 None。
        """
        try:
            parts = time_str.strip().split(":")
            if len(parts) != 2:
                return None
            hour = int(parts[0])
            minute = int(parts[1])
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return hour * 60 + minute
        except (ValueError, IndexError):
            pass
        return None
