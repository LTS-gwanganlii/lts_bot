"""Gemini Live API session manager for STT with session resumption and keep-alive.

- Connects to Live API (AI Studio) with optional session resumption handle.
- Sends 16 kHz PCM audio and collects transcript from server messages.
- Handles SessionResumptionUpdate: stores new_handle when resumable and present;
  otherwise treats as non-resumable and uses Plan B (new session on next connect).
- Receive loop runs in background; does not block event loop.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# Model and config (Live API native audio model)
LIVE_MODEL = "gemini-2.5-flash-native-audio-preview-12-2025"
AUDIO_PCM_MIME = "audio/pcm;rate=16000"
# 16 kHz, 16-bit mono: 32000 bytes/sec. 최소 100ms 미만은 non-audio 오류 유발 가능.
MIN_AUDIO_BYTES = 3200


class LiveSessionManager:
    """Manages Gemini Live API WebSocket session: connect, send audio, receive transcript and resumption."""

    def __init__(self, on_error: Callable[[str], None]) -> None:
        self._on_error = on_error
        self._client = None
        self._session = None
        self._session_cm = None  # async context manager for connect()
        self._receive_task: Optional[asyncio.Task] = None
        self._resumption_handle: Optional[str] = None
        self._transcript_queue: asyncio.Queue[str] = asyncio.Queue()
        self._lock = asyncio.Lock()
        self._closed = False

    def _get_client(self):
        if self._client is not None:
            return self._client
        try:
            from google import genai  # type: ignore
            api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
            if not api_key:
                raise RuntimeError("GEMINI_API_KEY (or GOOGLE_API_KEY) 환경 변수가 필요합니다.")
            self._client = genai.Client(api_key=api_key)
            return self._client
        except Exception as e:
            self._on_error(f"Gemini Live 클라이언트 초기화 실패: {e}")
            raise

    async def _receive_loop(self) -> None:
        """Consume session.receive(); push transcripts and resumption updates."""
        session = self._session
        if session is None:
            return
        try:
            async for msg in session.receive():
                if self._closed:
                    break
                # Session resumption: check resumable first, then new_handle
                if hasattr(msg, "session_resumption_update") and msg.session_resumption_update:
                    su = msg.session_resumption_update
                    resumable = getattr(su, "resumable", False) or (getattr(su, "resumable", None) is True)
                    new_handle = getattr(su, "new_handle", None) or getattr(su, "new_handle", "")
                    if resumable and new_handle:
                        self._resumption_handle = new_handle
                        logger.debug("Session resumption handle stored")
                    else:
                        # This segment not resumable or no handle
                        self._resumption_handle = None
                # Transcript: 입력 전사는 input_audio_transcription / server_content 또는 msg 최상위에 올 수 있음
                text = None
                def _str_from(val):
                    if val is None:
                        return None
                    if isinstance(val, str):
                        return val
                    if isinstance(val, bytes):
                        return val.decode("utf-8", errors="replace")
                    t = getattr(val, "text", None) or getattr(val, "content", None)
                    if t is not None:
                        return t if isinstance(t, str) else (t.decode("utf-8", errors="replace") if isinstance(t, bytes) else str(t))
                    if hasattr(val, "get"):
                        t = val.get("text") or val.get("content")
                        return t if isinstance(t, str) else (str(t) if t is not None else None)
                    return str(val) if val else None

                # 최상위 메시지 필드 (일부 SDK는 여기로 전사 전달)
                for key in ("input_audio_transcription", "inputAudioTranscription", "output_transcription", "outputTranscription", "text"):
                    val = getattr(msg, key, None)
                    if val is not None:
                        text = _str_from(val)
                        if text and text.strip():
                            break
                if text is None and hasattr(msg, "server_content") and msg.server_content:
                    sc = msg.server_content
                    text = _str_from(getattr(sc, "output_transcription", None) or getattr(sc, "outputTranscription", None))
                    if text is None:
                        inv = getattr(sc, "input_audio_transcription", None) or getattr(sc, "inputAudioTranscription", None)
                        if inv is not None:
                            text = _str_from(inv)
                    if text is None and hasattr(sc, "model_turn") and sc.model_turn and hasattr(sc.model_turn, "parts"):
                        for part in sc.model_turn.parts or []:
                            if getattr(part, "text", None):
                                text = part.text
                                break
                    if text is None and hasattr(msg, "text"):
                        text = msg.text
                if text is not None and not isinstance(text, str):
                    text = str(text) if text else None
                if text is not None and isinstance(text, str) and text.strip():
                    t = text.strip()
                    # 서버가 오류 메시지를 텍스트로 보낼 수 있음 (e.g. "Cannot extract voices from a non-audio request")
                    if "non-audio" in t.lower() or "cannot extract voices" in t.lower():
                        self._on_error(t)
                        self._transcript_queue.put_nowait("")
                    else:
                        self._transcript_queue.put_nowait(t)
                elif logger.isEnabledFor(logging.DEBUG) and hasattr(msg, "server_content") and msg.server_content:
                    # 전사 추출 실패 시 수신 구조 힌트 (DEBUG 레벨)
                    sc = msg.server_content
                    keys = [k for k in dir(sc) if not k.startswith("_")]
                    logger.debug("Live 수신: 전사 없음, server_content keys=%s", keys[:30])
        except asyncio.CancelledError:
            pass
        except Exception as e:
            if not self._closed:
                self._on_error(f"Live 수신 루프 오류: {e}")
            logger.exception("receive_loop")

    async def ensure_connected(self) -> bool:
        """Connect or reconnect; use resumption handle if available (and resumable)."""
        async with self._lock:
            if self._closed:
                return False
            if self._session is not None:
                return True
            client = self._get_client()
            # native-audio 모델은 response_modalities에 TEXT 미지원 → 1007 발생. AUDIO만 사용.
            # 입력 전사(STT)를 받으려면 input_audio_transcription 필요.
            config_dict: dict = {
                "response_modalities": ["AUDIO"],
                "output_audio_transcription": {},
                "input_audio_transcription": {},
            }
            if self._resumption_handle:
                config_dict["session_resumption"] = {"handle": self._resumption_handle}
            try:
                self._session_cm = client.aio.live.connect(
                    model=LIVE_MODEL,
                    config=config_dict,
                )
                self._session = await self._session_cm.__aenter__()
                self._receive_task = asyncio.create_task(self._receive_loop())
                logger.info("Live 세션 연결됨 (model=%s)", LIVE_MODEL)
                return True
            except Exception as e:
                self._on_error(f"Live 세션 연결 실패: {e}")
                self._resumption_handle = None
                self._session_cm = None
                self._session = None
                # 1007(잘못된 인자) 등 실패 시 재시도 1회(세션 재개 없이)
                try:
                    await asyncio.sleep(0.5)
                    config_retry = config_dict
                    self._session_cm = client.aio.live.connect(model=LIVE_MODEL, config=config_retry)
                    self._session = await self._session_cm.__aenter__()
                    self._receive_task = asyncio.create_task(self._receive_loop())
                    logger.info("Live 세션 재시도 연결됨")
                    return True
                except Exception:
                    return False

    async def close(self) -> None:
        """Close session and receive task."""
        self._closed = True
        if self._receive_task and not self._receive_task.done():
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass
        if self._session_cm is not None:
            try:
                await self._session_cm.__aexit__(None, None, None)
            except Exception:
                pass
            self._session_cm = None
            self._session = None

    async def transcribe(self, audio_bytes: bytes) -> str:
        """Send audio to Live session and wait for one transcript. Uses lock so only one send at a time."""
        if not audio_bytes or len(audio_bytes) < MIN_AUDIO_BYTES:
            return ""
        if self._session is None:
            logger.info("Live 세션 없음, 연결 시도...")
            ok = await self.ensure_connected()
            if not ok:
                return ""
        try:
            from google.genai import types  # type: ignore
            blob = types.Blob(data=audio_bytes, mime_type=AUDIO_PCM_MIME)
        except Exception as e:
            self._on_error(f"Live Blob 생성 실패: {e}")
            return ""
        async with self._lock:
            session = self._session
        if session is None:
            return ""
        logger.info("Live API 오디오 전송 중 (%d bytes)...", len(audio_bytes))
        try:
            # send_realtime_input: 한 번에 하나의 인자만 허용. 오디오 → 스트림 끝 순서로 별도 호출.
            await session.send_realtime_input(audio=blob)
            try:
                await session.send_realtime_input(audio_stream_end=True)
            except (TypeError, ValueError):
                pass  # audio_stream_end 단독 호출 미지원 시 무시
            logger.info("Live API 전송 완료, 전사 대기 중...")
        except AttributeError:
            try:
                await session.send(input=blob, end_of_turn=True)
            except Exception as e:
                self._on_error(f"Live 오디오 전송 실패: {e}")
                return ""
        except Exception as e:
            self._on_error(f"Live 오디오 전송 실패: {e}")
            async with self._lock:
                self._session = None
                if self._session_cm is not None:
                    try:
                        await self._session_cm.__aexit__(None, None, None)
                    except Exception:
                        pass
                    self._session_cm = None
            return ""
        try:
            text = await asyncio.wait_for(self._transcript_queue.get(), timeout=30.0)
            logger.info("Live API 전사 수신 완료")
            return text
        except asyncio.TimeoutError:
            self._on_error("Live 전사 응답 시간 초과")
            return ""
        except Exception as e:
            self._on_error(f"Live 전사 수신 실패: {e}")
            return ""
