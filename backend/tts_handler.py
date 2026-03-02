from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional


class TTSHandler:
    """MeloTTS speaker with language-specific voice routing and output lock."""

    VOICE_BY_LANG = {
        "ko": "KR",
        "en": "EN",
        "zh": "ZH",
        "ru": "RU",
    }

    def __init__(self, model_dir: str = "./models/melo") -> None:
        self._lock = threading.RLock()
        self._is_speaking = threading.Event()

        try:
            from melo.api import TTS  # type: ignore
        except Exception as exc:
            raise RuntimeError(f"MeloTTS 로드 실패: {exc}") from exc

        self._tts_cls = TTS
        self.model_dir = Path(model_dir)
        self._engines: dict[str, object] = {}

    def is_output_locked(self) -> bool:
        return self._is_speaking.is_set()

    def _get_engine(self, lang: str):
        lang = lang if lang in self.VOICE_BY_LANG else "ko"
        if lang not in self._engines:
            self._engines[lang] = self._tts_cls(language=lang, model_path=str(self.model_dir))
        return self._engines[lang]

    def speak(self, text: str, lang: str = "ko") -> None:
        if not text.strip():
            return
        with self._lock:
            self._is_speaking.set()
            try:
                engine = self._get_engine(lang)
                speaker = self.VOICE_BY_LANG.get(lang, "KR")
                # MeloTTS API can vary by version; this follows common synthesize call style.
                engine.tts_to_file(text=text, speaker=speaker, output_path="_tmp_tts.wav")
                # simple playback using sounddevice + soundfile
                import sounddevice as sd  # type: ignore
                import soundfile as sf  # type: ignore

                data, sr = sf.read("_tmp_tts.wav", dtype="float32")
                sd.play(data, sr)
                sd.wait()
            finally:
                self._is_speaking.clear()

    def speak_error(self, text: str) -> None:
        self.speak(f"오류: {text}", lang="ko")
