"""Gemini Live API session manager for STT with session resumption and keep-alive.

- Connects to Live API (AI Studio) with optional session resumption handle.
- Sends 16 kHz PCM audio and collects transcript from server messages.
- turn_complete 신호가 오면 해당 턴에서 누적된 전사 텍스트를 큐에 넣음.
  → 텍스트가 없어도 빈 문자열을 넣어 transcribe()가 타임아웃되지 않도록 함.
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
# 문서 권장: 20~40ms 청크로 전송 시 레이턴시/인식 안정성 유리. 20ms = 640 bytes.
CHUNK_BYTES = 640


def _raw_dump(obj, max_len: int = 500) -> str:
    """디버깅용: 객체를 로그 가능한 문자열로 (바이너리/과도한 길이 제한)."""
    if obj is None:
        return "None"
    if isinstance(obj, (str, bytes)):
        s = obj.decode("utf-8", errors="replace") if isinstance(obj, bytes) else obj
        return repr(s[:max_len] + ("..." if len(s) > max_len else ""))
    if hasattr(obj, "model_dump"):
        try:
            d = obj.model_dump(mode="json")
            return _raw_dump(d, max_len)
        except Exception:
            pass
    if hasattr(obj, "__dict__"):
        try:
            d = {k: getattr(obj, k, None) for k in dir(obj) if not k.startswith("_")}
            out = []
            for k, v in d.items():
                if isinstance(v, (bytes, bytearray)):
                    out.append(f"{k}=<bytes len={len(v)}>")
                elif isinstance(v, str) and len(v) > 200:
                    out.append(f"{k}={repr(v[:200])}...")
                else:
                    out.append(f"{k}={repr(v)[:150]}")
            return "{" + ", ".join(out[:20]) + (" ..." if len(out) > 20 else "") + "}"
        except Exception as e:
            return f"<dump err: {e}>"
    if isinstance(obj, dict):
        out = []
        for k, v in list(obj.items())[:15]:
            if isinstance(v, (bytes, bytearray)):
                out.append(f"{k}=<bytes len={len(v)}>")
            else:
                out.append(f"{k}={repr(v)[:100]}")
        return "{" + ", ".join(out) + (" ..." if len(obj) > 15 else "") + "}"
    return repr(obj)[:max_len]


def _extract_text(val) -> Optional[str]:
    """객체/str/bytes/dict에서 텍스트 문자열 추출."""
    if val is None:
        return None
    if isinstance(val, str):
        return val or None
    if isinstance(val, bytes):
        return val.decode("utf-8", errors="replace") or None
    # Pydantic 모델 / 일반 객체: .text 또는 .content 우선
    for attr in ("text", "content", "transcript"):
        t = getattr(val, attr, None)
        if t and isinstance(t, str):
            return t
        if t and isinstance(t, bytes):
            return t.decode("utf-8", errors="replace")
    # dict
    if hasattr(val, "get"):
        for key in ("text", "content", "transcript"):
            t = val.get(key)
            if t and isinstance(t, str):
                return t
    return None


class LiveSessionManager:
    """Manages Gemini Live API WebSocket session: connect, send audio, receive transcript and resumption."""

    def __init__(self, on_error: Callable[[str], None]) -> None:
        self._on_error = on_error
        self._client = None
        self._session = None
        self._session_cm = None
        self._receive_task: Optional[asyncio.Task] = None
        self._resumption_handle: Optional[str] = None
        # turn_complete마다 그 턴의 전사 결과를 넣는 큐 (str, 빈 문자열 포함)
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
        """Consume session.receive().
        - 한 턴의 전사 텍스트를 누적하고, turn_complete가 오면 큐에 넣음.
        - 텍스트가 없어도 turn_complete 시점에 빈 문자열을 넣어 transcribe()가 대기하지 않도록 함.
        """
        session = self._session
        if session is None:
            return

        # 현재 턴에서 누적된 전사 텍스트
        turn_texts: list[str] = []
        _msg_count = 0

        def _flush_turn():
            result = " ".join(turn_texts).strip()
            turn_texts.clear()
            logger.info("[Live RAW] _flush_turn → transcript_queue.put_nowait(%r)", result[:100] + ("..." if len(result) > 100 else ""))
            self._transcript_queue.put_nowait(result)
            logger.info("전사 턴 완료: %s", result[:80] + ("..." if len(result) > 80 else "") if result else "(없음)")

        logger.info("[Live RAW] receive_loop 시작, session.receive() 대기 중...")
        try:
            async for msg in session.receive():
                if self._closed:
                    break
                _msg_count += 1
                # RAW: 수신된 메시지 항상 로그 (타입 + 구조 요약)
                logger.info(
                    "[Live RAW] msg #%d 수신 | type=%s | dir=%s",
                    _msg_count,
                    type(msg).__name__,
                    [x for x in dir(msg) if not x.startswith("_")],
                )
                logger.info("[Live RAW] msg dump: %s", _raw_dump(msg))

                sc = getattr(msg, "server_content", None)
                if sc is not None:
                    logger.info(
                        "[Live RAW] server_content 있음 | sc.type=%s | sc.dump: %s",
                        type(sc).__name__,
                        _raw_dump(sc),
                    )
                    logger.info(
                        "[Live RAW] server_content turn_complete=%s, turnComplete=%s",
                        getattr(sc, "turn_complete", "<없음>"),
                        getattr(sc, "turnComplete", "<없음>"),
                    )

                # ── Session resumption ──────────────────────────────────────
                su = getattr(msg, "session_resumption_update", None)
                if su:
                    resumable = getattr(su, "resumable", False)
                    new_handle = getattr(su, "new_handle", None)
                    if resumable and new_handle:
                        self._resumption_handle = new_handle
                        logger.debug("Resumption handle updated")
                    else:
                        self._resumption_handle = None

                # ── 전사 텍스트 수집 ────────────────────────────────────────
                sc = getattr(msg, "server_content", None)
                if sc:
                    # input_audio_transcription / input_transcription: 사용자 음성 전사 (STT 핵심)
                    # 문서: https://ai.google.dev/gemini-api/docs/live
                    for attr in ("input_audio_transcription", "inputAudioTranscription", "input_transcription"):
                        t = _extract_text(getattr(sc, attr, None))
                        if t:
                            logger.debug("input_audio_transcription: %s", t)
                            turn_texts.append(t)
                            break

                    # output_transcription: 모델 음성 전사 (텍스트가 없을 때 보조)
                    if not turn_texts:
                        for attr in ("output_transcription", "outputTranscription"):
                            t = _extract_text(getattr(sc, attr, None))
                            if t:
                                logger.debug("output_transcription: %s", t)
                                turn_texts.append(t)
                                break

                    # model_turn.parts[].text (TEXT 모달리티 응답 혹은 일부 SDK 버전)
                    if not turn_texts:
                        model_turn = getattr(sc, "model_turn", None)
                        if model_turn:
                            for part in getattr(model_turn, "parts", None) or []:
                                t = getattr(part, "text", None)
                                if t:
                                    logger.debug("model_turn.part.text: %s", t)
                                    turn_texts.append(t)

                    # turn_complete / turnComplete → 큐에 넣기 (텍스트 없어도 빈 문자열로 신호)
                    turn_done = getattr(sc, "turn_complete", False) or getattr(sc, "turnComplete", False)
                    if turn_done:
                        logger.info("[Live RAW] turn_complete=True 감지 → _flush_turn() 호출")
                        _flush_turn()
                        continue

                    # 서버 오류 메시지 감지
                    raw_text = _extract_text(getattr(sc, "text", None)) or _extract_text(getattr(msg, "text", None))
                    if raw_text:
                        low = raw_text.lower()
                        if "non-audio" in low or "cannot extract voices" in low:
                            self._on_error(raw_text)
                            _flush_turn()  # 빈 문자열로 신호
                        elif raw_text not in turn_texts:
                            turn_texts.append(raw_text)

                # 메시지 최상위 text (일부 SDK 버전)
                top_text = _extract_text(getattr(msg, "text", None))
                if top_text and top_text not in turn_texts:
                    low = top_text.lower()
                    if "non-audio" in low or "cannot extract voices" in low:
                        self._on_error(top_text)
                        _flush_turn()
                    else:
                        turn_texts.append(top_text)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            if not self._closed:
                self._on_error(f"Live 수신 루프 오류: {e}")
            logger.exception("receive_loop error")

    async def ensure_connected(self) -> bool:
        """Connect or reconnect; use resumption handle if available."""
        async with self._lock:
            if self._closed:
                return False
            if self._session is not None:
                return True
            client = self._get_client()
            # native-audio 모델은 TEXT 미지원(→ 1007). AUDIO만 사용.
            # input_audio_transcription: 사용자 음성 전사 요청.
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
                # 재시도 1회 (세션 재개 없이)
                try:
                    await asyncio.sleep(0.5)
                    config_retry = {k: v for k, v in config_dict.items() if k != "session_resumption"}
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
        """Send audio to Live session and wait for turn_complete transcript."""
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
        import time as _time
        send_start = _time.perf_counter()
        total_bytes = len(audio_bytes)
        logger.info("[Live RAW] 오디오 전송 시작: %d bytes (%.2fs), mime=%s", total_bytes, total_bytes / 32000.0, AUDIO_PCM_MIME)

        try:
            # 문서 권장: 20~40ms 청크로 스트리밍. 한 번에 한 blob만 보내면 잘리거나 서버 처리 이슈 가능.
            chunks = []
            for i in range(0, total_bytes, CHUNK_BYTES):
                chunk = audio_bytes[i : i + CHUNK_BYTES]
                if chunk:
                    chunks.append(chunk)
            if not chunks:
                chunks = [audio_bytes]
            logger.info("[Live RAW] 청크 %d개로 전송 (청크당 최대 %d bytes ≈ 20ms)", len(chunks), CHUNK_BYTES)

            for idx, chunk in enumerate(chunks):
                chunk_blob = types.Blob(data=chunk, mime_type=AUDIO_PCM_MIME)
                await session.send_realtime_input(audio=chunk_blob)
                if idx == 0:
                    first_chunk_at = _time.perf_counter() - send_start
                    logger.info("[Live RAW] 첫 청크 전송 완료 (경과 %.3fs)", first_chunk_at)

            last_chunk_at = _time.perf_counter() - send_start
            logger.info("[Live RAW] 마지막 청크 전송 완료 (총 경과 %.3fs)", last_chunk_at)

            try:
                await session.send_realtime_input(audio_stream_end=True)
                stream_end_at = _time.perf_counter() - send_start
                logger.info("[Live RAW] audio_stream_end 전송 완료 (총 경과 %.3fs)", stream_end_at)
            except (TypeError, ValueError) as e:
                logger.info("[Live RAW] audio_stream_end 미지원 또는 오류 (무시): %s", e)
            logger.info("[Live RAW] 전송 끝. transcript_queue.get() 대기 (timeout=30s)...")
        except AttributeError as e:
            logger.info("[Live RAW] send_realtime_input 없음, send() 폴백 (한 번에 전송): %s", e)
            try:
                await session.send(input=blob, end_of_turn=True)
                logger.info("[Live RAW] session.send(end_of_turn=True) 완료 (경과 %.3fs)", _time.perf_counter() - send_start)
            except Exception as send_err:
                self._on_error(f"Live 오디오 전송 실패: {send_err}")
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
            logger.info("[Live RAW] transcript_queue.get() 수신: len=%d", len(text))
            logger.info("전사 결과: %s", text[:80] + "..." if len(text) > 80 else text)
            return text
        except asyncio.TimeoutError:
            logger.warning("[Live RAW] transcript_queue.get() 타임아웃(30s) — 서버에서 turn_complete 미수신 또는 메시지 없음")
            self._on_error("Live 전사 응답 시간 초과 (turn_complete 미수신)")
            return ""
        except Exception as e:
            self._on_error(f"Live 전사 수신 실패: {e}")
            return ""
