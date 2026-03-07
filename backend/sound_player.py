"""재생용 효과음(MP3) 재생. Gemini 호출 시 copy, 에러 시 error."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SOUNDS_DIR = Path(__file__).resolve().parent / "sounds"

# Windows: 콘솔 창 없이 백그라운드 실행용 (Python 3.7+)
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def play_sound(filename: str) -> None:
    """효과음 재생 (논블로킹, 백그라운드). 실패 시 무시."""
    path = SOUNDS_DIR / filename
    if not path.is_file():
        return
    try:
        if sys.platform == "win32":
            import winsound
            # 백그라운드에서 비동기로 파일 위치의 재생 (플래그: SND_FILENAME | SND_ASYNC)
            winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC)
        elif sys.platform == "darwin":
            subprocess.Popen(
                ["afplay", str(path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            # Linux 등: paplay, aplay, mpv 등
            for cmd in ["paplay", "aplay", "mpv", "ffplay"]:
                if os.path.exists(f"/usr/bin/{cmd}") or os.path.exists(f"/usr/local/bin/{cmd}"):
                    subprocess.Popen(
                        [cmd, str(path)],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    break
    except Exception:
        pass
