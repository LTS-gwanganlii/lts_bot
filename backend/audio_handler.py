"""Microphone capture + level/length gate + Gemini Live STT.

- PyAudio capture; no webrtcvad/whisper.
- Level gate: only buffer frames with RMS >= threshold.
- Min length gate: only treat as utterance when gated duration >= min_duration_sec; else discard.
- Utterance end: silence (e.g. 1.0s) after gated segment; then send buffer to LiveSessionManager.
- Half-duplex via is_output_locked callback.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import queue
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from live_session_manager import LiveSessionManager

logger = logging.getLogger(__name__)



class AudioHandler:
    """Microphone streaming + level/length gate + Gemini Live STT.

    - Uses PyAudio for capture.
    - Level gate: frames with RMS >= gate_threshold go into utterance buffer.
    - Min length: only send when gated segment duration >= gate_min_duration_sec; else discard.
    - Silence threshold (e.g. 1.0s) ends utterance and triggers STT via LiveSessionManager.
    - Half-duplex: when is_output_locked() is true, discard capture and flush buffer.
    """


    def __init__(
        self,
        on_error: Callable[[str], None],
        is_output_locked: Callable[[], bool],
        session_manager: LiveSessionManager,
        on_gemini_invoked: Optional[Callable[[], None]] = None,
        sample_rate: int = 16000,
        frame_ms: int = 30,
        silence_threshold: float = 1.0,
        gate_threshold: Optional[float] = None,
        gate_min_duration_sec: Optional[float] = None,
    ) -> None:
        self.on_error = on_error
        self.is_output_locked = is_output_locked
        self.session_manager = session_manager
        self.on_gemini_invoked = on_gemini_invoked
        self.sample_rate = sample_rate
        self.frame_ms = frame_ms
        self.frame_size = int(sample_rate * frame_ms / 1000)
        self.silence_threshold = silence_threshold
        self.gate_threshold = gate_threshold if gate_threshold is not None else self._env_float("AUDIO_GATE_THRESHOLD", 500.0)
        self.gate_min_duration_sec = gate_min_duration_sec if gate_min_duration_sec is not None else self._env_float("AUDIO_GATE_MIN_DURATION_SEC", 0.4)
        # STT 대기 중에는 캡처 큐에 넣지 않음 (큐 폭발 방지). _stt_busy.set() 시 캡처는 put 스킵.
        self._stt_busy = threading.Event()
        self._audio_queue: queue.Queue[bytes] = queue.Queue(maxsize=500)
        self._running = False
        self._thread: Optional[threading.Thread] = None

        try:
            import pyaudio  # type: ignore
            import webrtcvad # type: ignore
        except Exception as exc:
            raise RuntimeError(f"오디오 의존성 로드 실패: {exc}") from exc
        
        self._pyaudio_mod = pyaudio
        self._pa = self._pyaudio_mod.PyAudio()
        self.vad = webrtcvad.Vad(3)  # Aggressiveness: 3 (가장 강력하게 소음 차단)

    @staticmethod
    def _env_float(name: str, default: float) -> float:
        v = os.environ.get(name)
        if v is None:
            return default
        try:
            return float(v)
        except ValueError:
            return default

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        self._pa.terminate()

    def _capture_loop(self) -> None:
        try:
            stream = self._pa.open(
                format=self._pyaudio_mod.paInt16,
                channels=1,
                rate=self.sample_rate,
                input=True,
                frames_per_buffer=self.frame_size,
            )
        except Exception as exc:
            self.on_error(f"마이크 초기화 실패: {exc}")
            return

        while self._running:
            try:
                frame = stream.read(self.frame_size, exception_on_overflow=False)
                if self.is_output_locked():
                    continue
                if self._stt_busy.is_set():
                    continue
                self._audio_queue.put_nowait(frame)
            except queue.Full:
                _ = self._audio_queue.get_nowait()
            except Exception as exc:
                self.on_error(f"마이크 스트림 오류: {exc}")
                time.sleep(0.1)

        stream.stop_stream()
        stream.close()

    async def get_utterance_transcript_async(self) -> Optional[str]:
        """Wait for one utterance (level+length gate passed, then silence), then STT via LiveSessionManager."""
        voiced_frames: list[bytes] = []
        silence_sec = 0.0
        utterance_started = False  # True once gated duration >= gate_min_duration_sec
        frame_duration_sec = self.frame_ms / 1000.0
        voiced_duration_sec = 0.0

        while self._running:
            if self.is_output_locked():
                voiced_frames.clear()
                silence_sec = 0.0
                utterance_started = False
                voiced_duration_sec = 0.0
                while not self._audio_queue.empty():
                    try:
                        self._audio_queue.get_nowait()
                    except queue.Empty:
                        break
                await asyncio.sleep(0.03)
                continue

            try:
                frame = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: self._audio_queue.get(timeout=0.2),
                )
            except queue.Empty:
                await asyncio.sleep(0.02)
                continue
            except Exception:
                await asyncio.sleep(0.02)
                continue

            try:
                is_speech = self.vad.is_speech(frame, self.sample_rate)
            except Exception as e:
                is_speech = False

            if is_speech:
                if len(voiced_frames) == 0:
                    logger.info("게이트 임계 통과 (WebRTC VAD 감지)")
                voiced_frames.append(frame)
                silence_sec = 0.0
                voiced_duration_sec += frame_duration_sec
                if voiced_duration_sec >= self.gate_min_duration_sec:
                    utterance_started = True
                continue

            if voiced_frames:
                silence_sec += frame_duration_sec
                if silence_sec >= self.silence_threshold:
                    if utterance_started:
                        audio_bytes = b"".join(voiced_frames)
                        dur_sec = len(audio_bytes) / (self.sample_rate * 2)
                        queue_depth = self._audio_queue.qsize()
                        logger.info(
                            "오디오 수집완료 (%.2fs, %d bytes) 큐깊이=%d, copy 재생 후 STT 전송",
                            dur_sec, len(audio_bytes), queue_depth,
                        )
                        if self.on_gemini_invoked:
                            self.on_gemini_invoked()
                        logger.info("AI(STT) 호출 시작 (%.0f bytes)", len(audio_bytes))
                        self._stt_busy.set()
                        try:
                            text = await self.session_manager.transcribe(audio_bytes)
                        finally:
                            self._stt_busy.clear()
                        after_q = self._audio_queue.qsize()
                        if after_q >= 400:
                            logger.warning("STT 반환 후 큐 깊이=%d (거의 가득 — 다음 발화 앞부분 짤림 가능)", after_q)
                        if text:
                            logger.info("AI(STT) 응답 수신: %s", text[:80] + ("..." if len(text) > 80 else ""))
                        else:
                            logger.info("AI(STT) 응답 없음")
                        voiced_frames = []
                        silence_sec = 0.0
                        utterance_started = False
                        voiced_duration_sec = 0.0
                        if not text:
                            return None
                        return text.strip()
                    else:
                        voiced_frames.clear()
                        silence_sec = 0.0
                        voiced_duration_sec = 0.0

        return None
