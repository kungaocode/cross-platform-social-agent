from __future__ import annotations

from typing import Any


class OCRModule:
    """调用 wxauto Plus 图片消息自带的 OCR 接口。"""

    def recognize(self, message: Any, timeout: int = 3) -> dict[str, Any]:
        ocr = getattr(message, "ocr", None)
        if not callable(ocr):
            return {"success": False, "reason": "ocr_not_supported", "text": ""}
        try:
            result = ocr(timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - UI 自动化错误交回模型
            return {"success": False, "reason": f"ocr_error:{type(exc).__name__}:{exc}", "text": ""}
        if isinstance(result, dict) and (
            result.get("success") is False
            or result.get("status") not in (None, "成功", "success", "ok")
        ):
            return {"success": False, "reason": "ocr_failed", "text": ""}
        text = self._result_text(result)
        return {"success": bool(text), "reason": "" if text else "ocr_empty", "text": text}

    @staticmethod
    def _result_text(result: Any) -> str:
        """兼容 wxautox4 OcrResult、字符串和旧版字典返回值。"""
        if result is None:
            return ""
        if isinstance(result, str):
            return result.strip()
        if isinstance(result, dict):
            for key in ("text", "content", "result"):
                value = OCRModule._result_text(result.get(key))
                if value:
                    return value
            return ""
        for name in ("text", "content", "result"):
            try:
                value = getattr(result, name, None)
            except Exception:  # noqa: BLE001 - OCR 结果对象属性可能由扩展模块提供
                value = None
            if isinstance(value, str) and value.strip():
                return value.strip()
        if isinstance(result, (list, tuple)):
            return "\n".join(text for item in result if (text := OCRModule._result_text(item)))
        # 不把空结果对象、布尔值或错误结构的 repr 当作图片文字。
        return ""
