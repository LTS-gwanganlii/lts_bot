from __future__ import annotations

import json
import os
import time
import unicodedata
from collections import deque
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Deque, Dict


@dataclass
class ContextTurn:
    ts: float
    role: str
    text: str


class CommandNormalizer:
    """Normalize near-match Korean voice commands into canonical intents."""

    def __init__(self) -> None:
        self._phrase_aliases = {
            "음악 꺼줘": "음악 중지",
            "노래 꺼줘": "음악 중지",
            "재생 멈춰": "음악 중지",
            "음악 정지": "음악 중지",
            "다음 음악": "음악 다른거 틀어",
            "다음곡": "음악 다른거 틀어",
            "다른 노래": "음악 다른거 틀어",
            "스킵": "음악 다른거 틀어",
            "볼륨 줄여": "소리 줄여",
            "볼륨 높여": "소리 키워",
            "일시 정지": "잠깐 멈춰",
            "재생해": "틀어줘",
        }

    def normalize(self, text: str) -> str:
        cleaned = self._clean(text)
        if not cleaned:
            return text

        # exact alias replacement first
        for alias, canonical in self._phrase_aliases.items():
            if alias in cleaned:
                cleaned = cleaned.replace(alias, canonical)

        # fuzzy replacement for near-miss STT outputs
        tokens = cleaned.split()
        rebuilt: list[str] = []
        for token in tokens:
            replaced = self._best_alias(token)
            rebuilt.append(replaced if replaced else token)
        return " ".join(rebuilt)

    def _best_alias(self, token: str) -> str | None:
        best: tuple[float, str] = (0.0, "")
        for alias, canonical in self._phrase_aliases.items():
            score = SequenceMatcher(None, token, alias).ratio()
            if score > best[0]:
                best = (score, canonical)
        # conservative threshold to avoid overcorrection
        return best[1] if best[0] >= 0.74 else None

    def _clean(self, text: str) -> str:
        text = unicodedata.normalize("NFC", text)
        return " ".join(text.strip().split())


class LLMAgent:
    """Google Gemini wrapper with function-calling response normalization and short TTL memory."""

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

    def __init__(self, context_ttl_sec: int = 300, context_max_turns: int = 40) -> None:
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

        self._ttl_sec = context_ttl_sec
        self._memory: Deque[ContextTurn] = deque(maxlen=context_max_turns)
        self._normalizer = CommandNormalizer()

    def plan_action(self, user_text: str) -> Dict[str, Any]:
        normalized = self._normalizer.normalize(user_text)
        context_text = self._recent_context_text()
        prompt = (
            "사용자 명령을 아래 도구 스키마 중 하나로 변환하라. "
            "반드시 JSON만 출력. 불명확하면 action='none'.\n"
            "아래는 최근 대화 맥락이며 최신 의도를 우선한다.\n"
            f"CONTEXT={context_text}\n"
            f"NORMALIZED_USER={normalized}\n"
            f"TOOLS={json.dumps(self.TOOLS, ensure_ascii=False)}"
        )
        raw = self._model.generate_content(prompt).text or "{}"
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {"action": "none", "text": "명령을 이해하지 못했습니다."}

        self._remember("user", user_text)
        self._remember("normalized_user", normalized)
        self._remember("assistant_plan", json.dumps(parsed, ensure_ascii=False))
        return parsed

    def translate_text(self, text: str, target_lang: str) -> str:
        normalized = self._normalizer.normalize(text)
        context_text = self._recent_context_text()
        prompt = (
            "You are a real-time interpreter. Translate ONLY Korean phrases into target language. "
            "If input is not Korean or includes proper nouns/URLs, keep them as-is. "
            f"Target={target_lang}. Output translated text only.\n"
            f"RecentContext={context_text}\n"
            f"Input={normalized}"
        )
        out = self._model.generate_content(prompt).text
        translated = (out or "").strip()

        self._remember("user", text)
        self._remember("normalized_user", normalized)
        self._remember("assistant_translation", translated)
        return translated

    def _remember(self, role: str, text: str) -> None:
        self._prune_expired()
        self._memory.append(ContextTurn(ts=time.time(), role=role, text=text))

    def _prune_expired(self) -> None:
        now = time.time()
        while self._memory and (now - self._memory[0].ts) > self._ttl_sec:
            self._memory.popleft()

    def _recent_context_text(self) -> str:
        self._prune_expired()
        if not self._memory:
            return "[]"
        compact = [{"r": turn.role, "t": turn.text} for turn in self._memory]
        return json.dumps(compact, ensure_ascii=False)
