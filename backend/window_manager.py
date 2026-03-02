from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class MonitorTarget:
    left: tuple[int, int, int, int]
    right: tuple[int, int, int, int]


class WindowManager:
    """Win32 window controller for EDGE6.1 title target."""

    def __init__(self) -> None:
        try:
            import win32api  # type: ignore
            import win32con  # type: ignore
            import win32gui  # type: ignore
        except Exception as exc:
            raise RuntimeError(f"Win32 API 로드 실패: {exc}") from exc

        self.win32api = win32api
        self.win32con = win32con
        self.win32gui = win32gui
        self.targets = self._detect_monitor_targets()

    def _detect_monitor_targets(self) -> MonitorTarget:
        monitors = self.win32api.EnumDisplayMonitors()
        if len(monitors) < 2:
            raise RuntimeError("듀얼 모니터를 찾지 못했습니다.")
        rects = sorted([m[2] for m in monitors], key=lambda r: r[0])
        return MonitorTarget(left=rects[0], right=rects[-1])

    def _find_hwnd(self, title_keyword: str = "EDGE6.1") -> Optional[int]:
        found: list[int] = []

        def enum_cb(hwnd: int, _: int) -> None:
            if self.win32gui.IsWindowVisible(hwnd):
                title = self.win32gui.GetWindowText(hwnd)
                if title_keyword in title:
                    found.append(hwnd)

        self.win32gui.EnumWindows(enum_cb, 0)
        return found[0] if found else None

    def move_and_fullscreen(self, target: str) -> str:
        hwnd = self._find_hwnd()
        if not hwnd:
            raise RuntimeError("'EDGE6.1' 창을 찾을 수 없습니다.")

        rect = self.targets.left if target == "left" else self.targets.right
        x1, y1, x2, y2 = rect
        width, height = x2 - x1, y2 - y1

        self.win32gui.ShowWindow(hwnd, self.win32con.SW_RESTORE)
        self.win32gui.SetWindowPos(
            hwnd,
            self.win32con.HWND_TOP,
            x1,
            y1,
            width,
            height,
            self.win32con.SWP_SHOWWINDOW,
        )
        self.win32gui.SetForegroundWindow(hwnd)

        # Send F11 for fullscreen
        self.win32api.keybd_event(self.win32con.VK_F11, 0, 0, 0)
        self.win32api.keybd_event(self.win32con.VK_F11, 0, self.win32con.KEYEVENTF_KEYUP, 0)
        return f"EDGE6.1 창을 {target} 모니터로 이동하고 전체화면 처리했습니다."
