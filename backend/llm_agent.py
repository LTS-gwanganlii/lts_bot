from __future__ import annotations

import json
import os
from typing import Any, Dict


class LLMAgent:
    """Google Gemini wrapper with function-calling response normalization."""

    TOOLS = [
        {
            "name": "move_edge_window",
            "description": "Move EDGE6.1 window to specific monitor and fullscreen.",
            "parameters": {
                "type": "object",
                "properties": {"target": {"type": "string", "enum": ["left", "right"]}},
                "required": ["target"],
            },
        },
        {
            "name": "youtube_control",
            "description": "Control YouTube via chrome extension websocket bridge.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["search_and_play", "pause", "play", "seek"],
                    },
                    "query": {"type": "string"},
                    "seconds": {"type": "number"},
                },
                "required": ["action"],
            },
        },
    ]

    def __init__(self) -> None:
        key = os.getenv("GEMINI_API_KEY")
        if not key:
            raise RuntimeError("GEMINI_API_KEY 환경 변수가 필요합니다.")
        try:
            import google.generativeai as genai  # type: ignore
        except Exception as exc:
            raise RuntimeError(f"Gemini SDK 로드 실패: {exc}") from exc

        self._genai = genai
        self._genai.configure(api_key=key)
        self._model = self._genai.GenerativeModel("gemini-1.5-pro")

    def plan_action(self, user_text: str) -> Dict[str, Any]:
        prompt = (
            "사용자 명령을 아래 도구 스키마 중 하나로 변환하라. "
            "반드시 JSON만 출력. 불명확하면 action='none'.\n"
            f"TOOLS={json.dumps(self.TOOLS, ensure_ascii=False)}\n"
            f"USER={user_text}"
        )
        raw = self._model.generate_content(prompt).text or "{}"
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {"action": "none", "text": "명령을 이해하지 못했습니다."}
        return parsed

    def translate_text(self, text: str, target_lang: str) -> str:
        prompt = (
            "You are a real-time interpreter. Translate ONLY Korean phrases into target language. "
            "If input is not Korean or includes proper nouns/URLs, keep them as-is. "
            f"Target={target_lang}. Output translated text only. Input={text}"
        )
        out = self._model.generate_content(prompt).text
        return (out or "").strip()
