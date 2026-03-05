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

    def __init__(self, model_dir: Optional[str] = None) -> None:
        self._lock = threading.RLock()
        self._is_speaking = threading.Event()

        try:
            from melo.api import TTS  # type: ignore
        except Exception as exc:
            raise RuntimeError(f"MeloTTS 로드 실패: {exc}") from exc

        self._tts_cls = TTS
        # 최신 MeloTTS는 model_path 미지원. HF에서 자동 다운로드 또는 config_path/ckpt_path 사용.
        self._model_dir = Path(model_dir) if model_dir else None
        self._engines: dict[str, object] = {}

    def is_output_locked(self) -> bool:
        return self._is_speaking.is_set()

    def _get_engine(self, lang: str):
        lang = lang if lang in self.VOICE_BY_LANG else "ko"
        if lang not in self._engines:
            voice = self.VOICE_BY_LANG[lang]  # MeloTTS 언어 코드: KR, EN, ZH, RU
            if self._model_dir and (self._model_dir / "config.json").exists() and (self._model_dir / "checkpoint.pth").exists():
                self._engines[lang] = self._tts_cls(
                    language=voice,
                    device="auto",
                    use_hf=False,
                    config_path=str(self._model_dir / "config.json"),
                    ckpt_path=str(self._model_dir / "checkpoint.pth"),
                )
            else:
                self._engines[lang] = self._tts_cls(language=voice, device="auto")
        return self._engines[lang]

    def speak(self, text: str, lang: str = "ko") -> None:
        if not text.strip():
            return
        with self._lock:
            self._is_speaking.set()
            try:
                engine = self._get_engine(lang)
                # MeloTTS: speaker_id는 hps.data.spk2id에서 조회 (문자열 키 → 정수 ID)
                spk2id = getattr(getattr(engine, "hps", None), "data", None) and getattr(engine.hps.data, "spk2id", None)
                if spk2id:
                    voice = self.VOICE_BY_LANG.get(lang, "KR")
                    speaker_id = spk2id.get(voice, next(iter(spk2id.values())))
                else:
                    speaker_id = 0
                engine.tts_to_file(text=text, speaker_id=speaker_id, output_path="_tmp_tts.wav")
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
