from __future__ import annotations

import asyncio
import logging
import signal
import sys
from enum import Enum
from pathlib import Path

from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# 프로젝트 루트(backend 상위)의 .env 로드
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from audio_handler import AudioHandler
from live_session_manager import LiveSessionManager
from sound_player import play_sound
from tts_handler import TTSHandler
from websocket_server import WebSocketBridge
from window_manager import WindowManager


class VoiceAssistantApp:
    def __init__(self) -> None:
        self.tts = TTSHandler()
        self.live_session = LiveSessionManager(on_error=self._on_audio_error)
        self.audio = AudioHandler(
            on_error=self._on_audio_error,
            is_output_locked=self.tts.is_output_locked,
            session_manager=self.live_session,
            on_gemini_invoked=lambda: play_sound("copy.wav"),
        )

        try:
            self.windows = WindowManager()
        except Exception as exc:
            self.windows = None
            logger.exception("창 제어 기능 비활성화: %s", exc)
            play_sound("error.wav")
        self.ws_bridge = WebSocketBridge()

        self._running = True

    def _on_audio_error(self, text: str) -> None:
        logger.error("오디오/Live 오류: %s", text)
        play_sound("error.wav")

    async def _run_loop(self) -> None:
        await self.ws_bridge.start()
        self.audio.start()
        self.tts.speak("음성 비서가 시작되었습니다.", lang="ko")

        while self._running:
            try:
                result = await self.audio.get_utterance_transcript_async()
                if not result:
                    continue

                import json
                try:
                    data = json.loads(result)
                    if data.get("type") == "tool_call":
                        fcalls = data.get("data", {}).get("function_calls", [])
                        for fc in fcalls:
                            await self._execute_tool_call(fc)
                        continue
                except json.JSONDecodeError:
                    pass

                logger.info("AI(일반 응답): %s", result)
            except Exception as exc:
                logger.exception("런루프 예외: %s", exc)
                play_sound("error.wav")

        self.audio.stop()
        await self.live_session.close()
        await self.ws_bridge.stop()

    async def _execute_tool_call(self, fc: dict) -> None:
        name = fc.get("name")
        args = fc.get("args", {})
        logger.info("Tool Call 수신: %s(%s)", name, args)

        if name == "move_edge_window":
            if self.windows is None:
                logger.warning("창 제어 기능 비활성화 상태")
                return
            target = args.get("target", "left")
            msg = self.windows.move_and_fullscreen(target)
            logger.info("창 제어 결과: %s", msg)
            return

        if name == "youtube_control":
            payload = {
                "action": args.get("action", "play"),
                "query": args.get("query"),
                "seconds": args.get("seconds"),
            }
            await self.ws_bridge.broadcast(payload)
            logger.info("유튜브 제어 시그널 브로드캐스트 완료")
            return

        if name == "translate_speech":
            text = args.get("text")
            lang = args.get("target_lang", "ko")
            if text:
                logger.info("번역 텍스트 수신, TTS 재생 시작 (lang=%s): %s", lang, text)
                self.tts.speak(text, lang=lang)
            return


    def stop(self) -> None:
        self._running = False


def main() -> None:
    app = VoiceAssistantApp()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # add_signal_handler는 Unix 전용 (Windows 미지원)
    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, app.stop)

    try:
        loop.run_until_complete(app._run_loop())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
