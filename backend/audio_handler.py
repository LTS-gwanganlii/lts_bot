from __future__ import annotations

import queue
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass
class TranscriptResult:
    text: str
    is_wake_command: bool
    wake_payload: str
    translation_start_lang: Optional[str] = None
    translation_stop: bool = False


class AudioHandler:
    """Microphone streaming + VAD + whisper STT pipeline.

    - Uses PyAudio for capture.
    - Uses webrtcvad with explicit 1.0 second silence threshold.
    - Uses whisper-cpp-python bindings for transcription.
    - Supports half-duplex lock by checking `is_output_locked` callback.
    """

    WAKE_PATTERN = re.compile(r"\b(?:ok\s*홍걸|오케이\s*홍걸)\b", re.IGNORECASE)
    START_TRANSLATION_PATTERN = re.compile(
        r"(?:ok\s*홍걸|오케이\s*홍걸)\s*,?\s*(영어|러시아어|중국어)\s*번역\s*시작",
        re.IGNORECASE,
    )
    STOP_TRANSLATION_PATTERN = re.compile(r"번역\s*중지", re.IGNORECASE)

    LANG_MAP = {
        "영어": "en",
        "러시아어": "ru",
        "중국어": "zh",
    }

    def __init__(
        self,
        on_error: Callable[[str], None],
        is_output_locked: Callable[[], bool],
        sample_rate: int = 16000,
        frame_ms: int = 30,
        vad_mode: int = 2,
        silence_threshold: float = 1.0,
    ) -> None:
        self.on_error = on_error
        self.is_output_locked = is_output_locked
        self.sample_rate = sample_rate
        self.frame_ms = frame_ms
        self.frame_size = int(sample_rate * frame_ms / 1000)
        self.silence_threshold = silence_threshold
        self._audio_queue: queue.Queue[bytes] = queue.Queue(maxsize=120)
        self._running = False
        self._thread: Optional[threading.Thread] = None

        try:
            import pyaudio  # type: ignore
            import webrtcvad  # type: ignore
        except Exception as exc:
            raise RuntimeError(f"오디오 의존성 로드 실패: {exc}") from exc

        self._pyaudio_mod = pyaudio
        self._vad = webrtcvad.Vad(vad_mode)
        self._pa = self._pyaudio_mod.PyAudio()

        try:
            from whisper_cpp_python import Whisper  # type: ignore

            self._stt = Whisper.from_pretrained("ggml-base.bin")
        except Exception as exc:
            raise RuntimeError(f"Whisper 모델 로드 실패: {exc}") from exc

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
                    # Half-duplex: during TTS playback mic input is discarded.
                    continue
                self._audio_queue.put_nowait(frame)
            except queue.Full:
                # drop oldest behavior by discarding silently
                _ = self._audio_queue.get_nowait()
            except Exception as exc:
                self.on_error(f"마이크 스트림 오류: {exc}")
                time.sleep(0.1)

        stream.stop_stream()
        stream.close()

    def get_utterance_transcript(self, translation_mode: bool) -> Optional[TranscriptResult]:
        """Block until one utterance finalized by VAD silence>=1.0s then STT."""
        voiced_frames: list[bytes] = []
        silence_sec = 0.0

        while self._running:
            if self.is_output_locked():
                # flush queue entirely while output is speaking
                while not self._audio_queue.empty():
                    try:
                        self._audio_queue.get_nowait()
                    except queue.Empty:
                        break
                time.sleep(0.03)
                continue

            try:
                frame = self._audio_queue.get(timeout=0.2)
            except queue.Empty:
                continue

            is_speech = self._vad.is_speech(frame, self.sample_rate)
            if is_speech:
                voiced_frames.append(frame)
                silence_sec = 0.0
                continue

            if voiced_frames:
                silence_sec += self.frame_ms / 1000.0
                if silence_sec >= self.silence_threshold:
                    text = self._transcribe(voiced_frames)
                    voiced_frames = []
                    silence_sec = 0.0
                    if not text:
                        return None
                    return self._parse_text(text, translation_mode)

        return None

    def _transcribe(self, frames: list[bytes]) -> str:
        try:
            audio_bytes = b"".join(frames)
            result = self._stt.transcribe(audio_bytes, sample_rate=self.sample_rate)
            text = (result.get("text") or "").strip()
            return text
        except Exception as exc:
            self.on_error(f"음성 인식 실패: {exc}")
            return ""

    def _parse_text(self, text: str, translation_mode: bool) -> TranscriptResult:
        text = text.strip()

        if self.STOP_TRANSLATION_PATTERN.search(text):
            return TranscriptResult(text=text, is_wake_command=False, wake_payload="", translation_stop=True)

        start_match = self.START_TRANSLATION_PATTERN.search(text)
        if start_match:
            lang_kr = start_match.group(1)
            return TranscriptResult(
                text=text,
                is_wake_command=True,
                wake_payload=text,
                translation_start_lang=self.LANG_MAP.get(lang_kr),
            )

        if translation_mode:
            return TranscriptResult(text=text, is_wake_command=True, wake_payload=text)

        wake_match = self.WAKE_PATTERN.search(text)
        if wake_match:
            payload = text[wake_match.end() :].lstrip(" ,")
            return TranscriptResult(text=text, is_wake_command=True, wake_payload=payload)

        return TranscriptResult(text=text, is_wake_command=False, wake_payload="")
