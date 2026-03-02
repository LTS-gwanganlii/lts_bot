from __future__ import annotations

import asyncio
import signal
from enum import Enum

from audio_handler import AudioHandler
from llm_agent import LLMAgent
from tts_handler import TTSHandler
from websocket_server import WebSocketBridge
from window_manager import WindowManager


class AssistantMode(str, Enum):
    NORMAL_MODE = "NORMAL_MODE"
    TRANSLATION_MODE = "TRANSLATION_MODE"


class VoiceAssistantApp:
    def __init__(self) -> None:
        self.tts = TTSHandler()
        self.audio = AudioHandler(on_error=self._on_audio_error, is_output_locked=self.tts.is_output_locked)
        self.llm = LLMAgent()
        self.windows = WindowManager()
        self.ws_bridge = WebSocketBridge()

        self.mode = AssistantMode.NORMAL_MODE
        self.translation_target_lang = "en"
        self._running = True

    def _on_audio_error(self, text: str) -> None:
        self.tts.speak_error(text)

    async def _run_loop(self) -> None:
        await self.ws_bridge.start()
        self.audio.start()
        self.tts.speak("음성 비서가 시작되었습니다.", lang="ko")

        while self._running:
            try:
                result = self.audio.get_utterance_transcript(
                    translation_mode=self.mode == AssistantMode.TRANSLATION_MODE
                )
                if result is None:
                    continue

                if result.translation_stop:
                    self.mode = AssistantMode.NORMAL_MODE
                    self.tts.speak("번역 모드를 종료합니다.", lang="ko")
                    continue

                if result.translation_start_lang:
                    self.mode = AssistantMode.TRANSLATION_MODE
                    self.translation_target_lang = result.translation_start_lang
                    self.tts.speak("번역 모드를 시작합니다.", lang="ko")
                    continue

                if self.mode == AssistantMode.TRANSLATION_MODE:
                    translated = self.llm.translate_text(result.text, self.translation_target_lang)
                    self.tts.speak(translated, lang=self.translation_target_lang)
                    continue

                if not result.is_wake_command:
                    continue

                action = self.llm.plan_action(result.wake_payload)
                await self._execute_action(action)
            except Exception as exc:
                self.tts.speak_error(str(exc))

        self.audio.stop()
        await self.ws_bridge.stop()

    async def _execute_action(self, action: dict) -> None:
        kind = action.get("action", "none")

        if kind == "move_edge_window":
            target = action.get("target", "left")
            msg = self.windows.move_and_fullscreen(target)
            self.tts.speak(msg, lang="ko")
            return

        if kind == "youtube_control":
            payload = {
                "action": action.get("action_name") or action.get("youtube_action") or action.get("sub_action") or action.get("action"),
                "query": action.get("query"),
                "seconds": action.get("seconds"),
            }
            if payload["action"] == "youtube_control":
                payload["action"] = action.get("youtube_action", "play")
            await self.ws_bridge.broadcast(payload)
            self.tts.speak("유튜브 명령을 전달했습니다.", lang="ko")
            return

        self.tts.speak(action.get("text", "명령을 이해하지 못했습니다."), lang="ko")

    def stop(self) -> None:
        self._running = False


def main() -> None:
    app = VoiceAssistantApp()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, app.stop)

    try:
        loop.run_until_complete(app._run_loop())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
