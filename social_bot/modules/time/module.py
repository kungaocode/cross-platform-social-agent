from __future__ import annotations

from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Any


class TimeModule:
    """提供带时区的当前日期和时间，供模型和天气查询共用。"""

    def __init__(self, timezone_name: str = "Asia/Shanghai") -> None:
        self.timezone_name = str(timezone_name or "Asia/Shanghai").strip() or "Asia/Shanghai"
        try:
            self._timezone = ZoneInfo(self.timezone_name)
        except ZoneInfoNotFoundError:
            # Windows 精简环境可能没有时区数据库；中国项目固定使用 UTC+8 兜底。
            self._timezone = timezone(timedelta(hours=8), name="UTC+08:00")
            self.timezone_name = "Asia/Shanghai"

    def now(self, current: datetime | None = None) -> dict[str, Any]:
        value = current or datetime.now(self._timezone)
        if value.tzinfo is None:
            value = value.replace(tzinfo=self._timezone)
        else:
            value = value.astimezone(self._timezone)
        weekdays = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")
        return {
            "success": True,
            "timezone": self.timezone_name,
            "iso": value.isoformat(timespec="seconds"),
            "date": value.strftime("%Y-%m-%d"),
            "time": value.strftime("%H:%M:%S"),
            "weekday": weekdays[value.weekday()],
            "timestamp": value.timestamp(),
        }
