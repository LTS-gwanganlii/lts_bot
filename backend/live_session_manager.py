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


def _msg_summary(msg) -> str:
    """수신 메시지 전체 요약: 모든 비공개 아닌 필드명과 타입/요약값 (activity_end, usage_metadata 등 포함)."""
    if msg is None:
        return "None"
    parts = []
    for k in dir(msg):
        if k.startswith("_"):
            continue
        try:
            v = getattr(msg, k, None)
            if callable(v):
                continue
            if isinstance(v, (bytes, bytearray)):
                parts.append(f"{k}=<bytes len={len(v)}>")
            elif isinstance(v, str) and len(v) > 100:
                parts.append(f"{k}={repr(v[:100])}...")
            else:
                parts.append(f"{k}={repr(v)[:120]}")
        except Exception as e:
            parts.append(f"{k}=<err:{e}>")
    return " | ".join(parts[:25])


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
        # 전사 스트리밍: 전사 업데이트마다 put (turn_complete 의존 안 함). transcribe()는 첫 이벤트 + 0.8s idle로 확정.
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

        turn_texts: list[str] = []
        _msg_count = 0

        def _put_transcript(text: str) -> None:
            """전사 스트리밍: 업데이트마다 큐에 넣음 (turn_complete 의존 안 함)."""
            if text:
                self._transcript_queue.put_nowait(text)
                logger.debug("[Live] 전사 업데이트: %s", text[:60] + ("..." if len(text) > 60 else ""))

        def _on_turn_complete():
            result = " ".join(turn_texts).strip()
            turn_texts.clear()
            logger.info("[Live RAW] turn_complete → 턴 종료 (로그만, 큐는 스트리밍으로 이미 반영): %s", result[:80] if result else "(없음)")

        logger.info("[Live RAW] receive_loop 시작 (턴 끝나면 receive() 재호출)...")
        try:
            while not self._closed and session is self._session:
                async for msg in session.receive():
                    if self._closed:
                        break
                        _msg_count += 1
                        # RAW: 수신된 메시지 전체 로깅 — transcript 외 activity_end, usage_metadata, AUDIO 등 누락 방지
                        attrs = [x for x in dir(msg) if not x.startswith("_")]
                        logger.info(
                        "[Live RAW] msg #%d 수신 | type=%s | attrs=%s",
                        _msg_count,
                        type(msg).__name__,
                        attrs,
                        )
                        logger.info("[Live RAW] msg 전체 필드 요약: %s", _msg_summary(msg))
                        logger.info("[Live RAW] msg dump: %s", _raw_dump(msg))
                        if hasattr(msg, "model_dump"):
                            try:
                                wire = msg.model_dump(mode="json")
                                # 바이너리/긴 값은 길이만
                                def _wire_summary(d, depth=0):
                                    if depth > 2:
                                        return "..."
                                    if isinstance(d, dict):
                                        return {k: _wire_summary(v, depth + 1) for k, v in list(d.items())[:12]}
                                    if isinstance(d, (bytes, bytearray)):
                                        return f"<bytes len={len(d)}>"
                                    if isinstance(d, str) and len(d) > 80:
                                        return d[:80] + "..."
                                    return d
                                logger.info("[Live RAW] msg.model_dump (와이어 포맷 키): %s", list(wire.keys()) if isinstance(wire, dict) else type(wire).__name__)
                                logger.info("[Live RAW] msg.model_dump 요약: %s", _raw_dump(_wire_summary(wire)))
                            except Exception as e:
                                logger.info("[Live RAW] msg.model_dump 실패: %s", e)

                    # 문서상 서버 메시지 타입: ServerContent, ToolCall, ActivityEnd, GoAway, usageMetadata 등
                    for wire_name in ("activity_end", "activityEnd", "usage_metadata", "usageMetadata", "go_away", "goAway"):
                        v = getattr(msg, wire_name, None)
                        if v is not None:
                            logger.info("[Live RAW] msg.%s = %s", wire_name, _raw_dump(v))

                    sc = getattr(msg, "server_content", None) or getattr(msg, "serverContent", None)
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
                        # AUDIO/바이너리 파트: parts, inline_data 등
                        for part_attr in ("parts", "model_turn", "modelTurn"):
                            p = getattr(sc, part_attr, None)
                            if p is None:
                                continue
                            if hasattr(p, "__iter__") and not isinstance(p, (str, bytes)):
                                for i, part in enumerate(list(p)[:5]):
                                    pd = _raw_dump(part)
                                    if "bytes" in pd or "data" in pd:
                                        logger.info("[Live RAW] server_content.%s[%d] (바이너리/파트): %s", part_attr, i, pd)

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
                                _put_transcript(t)
                                break

                        # output_transcription: 모델 음성 전사 (텍스트가 없을 때 보조)
                        if not turn_texts:
                            for attr in ("output_transcription", "outputTranscription"):
                                t = _extract_text(getattr(sc, attr, None))
                                if t:
                                    logger.debug("output_transcription: %s", t)
                                    turn_texts.append(t)
                                    _put_transcript(t)
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
                                        _put_transcript(t)

                        # turn_complete 시 스트림 종료(이 for 끝남). 다음 턴은 바깥 while에서 receive() 재호출.
                        turn_done = getattr(sc, "turn_complete", False) or getattr(sc, "turnComplete", False)
                        if turn_done:
                            logger.info("[Live RAW] turn_complete=True 감지 → 턴 종료")
                            _on_turn_complete()
                            continue

                        # 서버 오류 메시지 감지
                        raw_text = _extract_text(getattr(sc, "text", None)) or _extract_text(getattr(msg, "text", None))
                        if raw_text:
                            low = raw_text.lower()
                            if "non-audio" in low or "cannot extract voices" in low:
                                self._on_error(raw_text)
                                self._transcript_queue.put_nowait("")  # 대기 중인 transcribe() 해제
                                _on_turn_complete()
                            elif raw_text not in turn_texts:
                                turn_texts.append(raw_text)
                                _put_transcript(raw_text)

                    # 메시지 최상위 text (일부 SDK 버전)
                    top_text = _extract_text(getattr(msg, "text", None))
                    if top_text and top_text not in turn_texts:
                        low = top_text.lower()
                        if "non-audio" in low or "cannot extract voices" in low:
                            self._on_error(top_text)
                            self._transcript_queue.put_nowait("")
                            _on_turn_complete()
                        else:
                            turn_texts.append(top_text)
                            _put_transcript(top_text)

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
            # 서버 VAD 끄고 클라이언트가 activity_start/activity_end로 턴 경계 명시.
            config_dict: dict = {
                "response_modalities": ["AUDIO"],
                "output_audio_transcription": {},
                "input_audio_transcription": {},
                "realtime_input_config": {
                    "automatic_activity_detection": {"disabled": True},
                },
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
        """Send audio with activity_start/activity_end 경계, 전사는 스트리밍 수신 후 첫 이벤트 + 0.8s idle로 확정."""
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

        # 이번 턴에 해당하지 않는 이전 전사 제거
        while True:
            try:
                self._transcript_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        import time as _time
        send_start = _time.perf_counter()
        total_bytes = len(audio_bytes)
        logger.info("[Live RAW] 오디오 전송 시작: %d bytes (%.2fs), mime=%s", total_bytes, total_bytes / 32000.0, AUDIO_PCM_MIME)

        try:
            # 턴 경계: activity_start → 오디오 → activity_end (서버 VAD 끄고 클라이언트가 경계 명시)
            async def _send_activity_start() -> None:
                send_fn = getattr(session, "send_realtime_input", None)
                if not send_fn:
                    return
                try:
                    try:
                        from google.genai.types import ActivityStart
                        await send_fn(activity_start=ActivityStart())
                    except (ImportError, AttributeError, TypeError):
                        await send_fn(activity_start=True)
                    logger.info("[Live RAW] activity_start 전송 완료")
                except (TypeError, ValueError, AttributeError) as e:
                    logger.debug("[Live RAW] activity_start 미지원(무시): %s", e)

            async def _send_activity_end() -> None:
                send_fn = getattr(session, "send_realtime_input", None)
                if not send_fn:
                    return
                try:
                    try:
                        from google.genai.types import ActivityEnd
                        await send_fn(activity_end=ActivityEnd())
                    except (ImportError, AttributeError, TypeError):
                        await send_fn(activity_end=True)
                    logger.info("[Live RAW] activity_end 전송 완료 (총 경과 %.3fs)", _time.perf_counter() - send_start)
                except (TypeError, ValueError, AttributeError) as e:
                    logger.debug("[Live RAW] activity_end 미지원(무시): %s", e)

            await _send_activity_start()

            # 문서 권장: 20~40ms 청크로 스트리밍
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

            # 턴 종료: activity_end (스트림 끝을 서버에 명시)
            await _send_activity_end()

            logger.info("[Live RAW] 전송 끝. 전사 스트리밍 대기 (첫 이벤트 + 0.8s idle)...")
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
        # 전사 스트리밍: 첫 이벤트까지 30s 대기, 이후 0.8s idle이면 최종값 확정 반환
        try:
            first = await asyncio.wait_for(self._transcript_queue.get(), timeout=30.0)
            last_text = first.strip()
            idle_sec = 0.8
            deadline = _time.perf_counter() + idle_sec
            while _time.perf_counter() < deadline:
                try:
                    more = self._transcript_queue.get_nowait()
                    last_text = (more or "").strip() or last_text
                    deadline = _time.perf_counter() + idle_sec
                except asyncio.QueueEmpty:
                    await asyncio.sleep(0.05)
            logger.info("[Live RAW] 전사 확정 (0.8s idle): len=%d", len(last_text))
            logger.info("전사 결과: %s", last_text[:80] + "..." if len(last_text) > 80 else last_text)
            return last_text
        except asyncio.TimeoutError:
            logger.warning("[Live RAW] 첫 전사 이벤트 30s 타임아웃")
            self._on_error("Live 전사 응답 시간 초과")
            return ""
        except Exception as e:
            self._on_error(f"Live 전사 수신 실패: {e}")
            return ""
