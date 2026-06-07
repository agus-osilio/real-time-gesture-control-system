"""
╔══════════════════════════════════════════════════════════════╗
║         HAND GESTURE WINDOW CONTROLLER — v2.1               ║
║                                                              ║
║  GESTURES:                                                   ║
║    ✊  Fist over window        → Grab & move                ║
║    ✌️   V-sign over window     → Minimize                   ║
║    🤲  Open 2nd hand (GRAB)   → Scale/resize               ║
║    ✌️   V-sign other hand      → Send to next monitor       ║
║    ✊✊ Both fists (hold ~1s)  → Close grabbed window       ║
║    🖐️  Open both palms         → Release                   ║
╚══════════════════════════════════════════════════════════════╝
"""

import ctypes
import subprocess
import cv2
import mediapipe as mp
import numpy as np
import win32gui
import win32con
import win32api
import time
import threading
from dataclasses import dataclass
from typing import Optional, Tuple, List
from enum import Enum, auto


# ─────────────────────────────────────────────────────────────
#  CONFIGURATION
# ─────────────────────────────────────────────────────────────

WEBCAM_INDEX          = 0
FRAME_WIDTH           = 1280
FRAME_HEIGHT          = 720
SMOOTHING_ALPHA       = 0.35
GESTURE_HOLD_FRAMES   = 4
CLOSE_HOLD_FRAMES     = 18   # ~0.6s at 30fps — intentionally long (destructive)
SCALE_HOLD_FRAMES     = 23   # ~0.75s at 30fps — hold open palm to confirm scale intent
MONITOR_TARGET_FRAMES = 45   # 1.5s at 30fps — hold finger gesture to switch monitor
HAND_DETECT_GRACE     = 3    # inference frames to keep a hand's last state when MediaPipe
                             # briefly loses it — prevents 1-frame drops from interrupting grabs
MIN_WINDOW_WIDTH      = 300
MIN_WINDOW_HEIGHT     = 200
MAX_WINDOW_WIDTH      = 3840
MAX_WINDOW_HEIGHT     = 2160
SCALE_SENSITIVITY     = 1.4
MOVE_DEADZONE_PX      = 3
ACTIVE_MARGIN         = 0.12   # Crop outer 12% of camera frame; center band maps to full monitor
DEBUG_OVERLAY         = True

EXCLUDED_TITLES = [
    "", "Program Manager", "Windows Input Experience",
    "gesture_window_controller", "GestureOverlay",
]


# ─────────────────────────────────────────────────────────────
#  COLOR HELPERS  (Win32 COLORREF = R | G<<8 | B<<16)
# ─────────────────────────────────────────────────────────────

def _rgb(r: int, g: int, b: int) -> int:
    return r | (g << 8) | (b << 16)

_COL_CHROMA = _rgb(1,   2,   3)    # transparency key (near-black, never used by GDI)
_COL_GREEN  = _rgb(100, 255, 100)  # left-hand cursor
_COL_BLUE   = _rgb(100, 150, 255)  # right-hand cursor
_COL_WHITE  = _rgb(255, 255, 255)
_COL_CYAN   = _rgb(50,  200, 255)
_COL_GRAY   = _rgb(170, 170, 170)
_COL_RED    = _rgb(255,  50,  50)


# ─────────────────────────────────────────────────────────────
#  ENUMS & DATACLASSES
# ─────────────────────────────────────────────────────────────

class AppState(Enum):
    IDLE  = auto()
    GRAB  = auto()
    SCALE = auto()


@dataclass
class HandState:
    landmarks:    Optional[np.ndarray] = None
    smooth_x:     float = 0.0
    smooth_y:     float = 0.0
    is_fist:      bool  = False
    is_open:      bool  = False
    is_one:       bool  = False   # ☝️  only index extended
    is_two:       bool  = False   # ✌️  index + middle only
    is_three:     bool  = False   # 🤟  index + middle + ring only
    fist_frames:  int   = 0
    open_frames:  int   = 0
    one_frames:   int   = 0
    two_frames:   int   = 0
    three_frames: int   = 0
    miss_frames:  int   = 0   # consecutive frames with unrecognized gesture (hand visible)
    detect_miss:  int   = 0   # consecutive inference frames where hand was not detected at all
    initialized:  bool  = False


@dataclass
class WindowSnapshot:
    hwnd:             int   = 0
    title:            str   = ""
    origin_win_x:     int   = 0
    origin_win_y:     int   = 0
    origin_hand_x:    float = 0.0
    origin_hand_y:    float = 0.0
    width:            int   = 0
    height:           int   = 0
    base_span:        float = 0.0
    base_width:       int   = 0
    base_height:      int   = 0
    grab_side:        str   = 'R'
    both_fist_frames:  int  = 0
    scale_hold_frames: int  = 0


# ─────────────────────────────────────────────────────────────
#  GESTURE DETECTOR
# ─────────────────────────────────────────────────────────────

class GestureDetector:
    FINGER_TIPS = [8, 12, 16, 20]
    FINGER_MCPS = [5,  9, 13, 17]

    @staticmethod
    def _dist2d(a: np.ndarray, b: np.ndarray) -> float:
        return float(np.linalg.norm(a[:2] - b[:2]))

    @classmethod
    def _extended(cls, lm: np.ndarray, tip: int, mcp: int) -> bool:
        return cls._dist2d(lm[tip], lm[0]) > cls._dist2d(lm[mcp], lm[0]) * 1.1

    @classmethod
    def classify(cls, lm: np.ndarray) -> Tuple[bool, bool, bool, bool, bool]:
        """Returns (is_fist, is_open, is_one, is_two, is_three).

        ext = [index, middle, ring, pinky]:
            is_fist  — <=1 finger and NOT the index (pure closed fist)
            is_one   — only index extended       (☝️)
            is_two   — index + middle only       (✌️)
            is_three — index + middle + ring     (three fingers)
            is_open  — all 4 fingers extended
        """
        ext = [cls._extended(lm, t, m)
               for t, m in zip(cls.FINGER_TIPS, cls.FINGER_MCPS)]
        count    = sum(ext)
        is_fist  = count <= 1 and not ext[0]
        is_open  = count >= 4
        is_one   =     ext[0] and not ext[1] and not ext[2] and not ext[3]
        is_two   =     ext[0] and     ext[1] and not ext[2] and not ext[3]
        is_three =     ext[0] and     ext[1] and     ext[2] and not ext[3]
        return is_fist, is_open, is_one, is_two, is_three


# ─────────────────────────────────────────────────────────────
#  MONITOR MANAGER
# ─────────────────────────────────────────────────────────────

class MonitorManager:
    def __init__(self):
        self.monitors: List[Tuple[int, int, int, int]] = []
        self.virtual_left   = 0
        self.virtual_top    = 0
        self.virtual_width  = 0
        self.virtual_height = 0
        self._enumerate()

    def _enumerate(self):
        try:
            raw = win32api.EnumDisplayMonitors(None, None)
            rects = [(l, t, r - l, b - t) for _, _, (l, t, r, b) in raw]
        except Exception:
            rects = [(0, 0, win32api.GetSystemMetrics(0), win32api.GetSystemMetrics(1))]
        self.monitors = rects
        all_l = min(r[0] for r in rects)
        all_t = min(r[1] for r in rects)
        all_r = max(r[0] + r[2] for r in rects)
        all_b = max(r[1] + r[3] for r in rects)
        self.virtual_left   = all_l
        self.virtual_top    = all_t
        self.virtual_width  = all_r - all_l
        self.virtual_height = all_b - all_t

    @property
    def primary(self) -> Tuple[int, int, int, int]:
        return self.monitors[0] if self.monitors else (
            0, 0, win32api.GetSystemMetrics(0), win32api.GetSystemMetrics(1)
        )

    def monitor_for(self, x: int, y: int) -> int:
        for i, (mx, my, mw, mh) in enumerate(self.monitors):
            if mx <= x < mx + mw and my <= y < my + mh:
                return i
        return 0

    def next_monitor(self, idx: int) -> int:
        return (idx + 1) % len(self.monitors)

    def move_window_to_monitor(self, hwnd: int, target_idx: int):
        mx, my, mw, mh = self.monitors[target_idx]
        try:
            rect = win32gui.GetWindowRect(hwnd)
            ww = rect[2] - rect[0]
            wh = rect[3] - rect[1]
            new_x = mx + (mw - ww) // 2
            new_y = my + (mh - wh) // 2
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            win32gui.MoveWindow(hwnd, new_x, new_y, ww, wh, True)
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────
#  WINDOW MANAGER
# ─────────────────────────────────────────────────────────────

class WindowManager:
    @staticmethod
    def find_window_at(x: int, y: int) -> Optional[Tuple[int, str, tuple]]:
        hwnd = win32gui.WindowFromPoint((x, y))
        hwnd = win32gui.GetAncestor(hwnd, win32con.GA_ROOT)
        if not hwnd:
            return None
        title = win32gui.GetWindowText(hwnd)
        if not title or title in EXCLUDED_TITLES:
            return None
        try:
            return hwnd, title, win32gui.GetWindowRect(hwnd)
        except Exception:
            return None

    @staticmethod
    def move_window(hwnd: int, x: int, y: int, w: int, h: int):
        w = max(MIN_WINDOW_WIDTH,  min(int(w), MAX_WINDOW_WIDTH))
        h = max(MIN_WINDOW_HEIGHT, min(int(h), MAX_WINDOW_HEIGHT))
        try:
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            win32gui.MoveWindow(hwnd, int(x), int(y), w, h, True)
        except Exception:
            pass

    @staticmethod
    def bring_to_front(hwnd: int):
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            pass

    @staticmethod
    def minimize_window(hwnd: int):
        try:
            win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
        except Exception:
            pass

    @staticmethod
    def close_window(hwnd: int):
        try:
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────
#  SCREEN MAPPER
# ─────────────────────────────────────────────────────────────

class ScreenMapper:
    def __init__(self, mm: MonitorManager, active_idx: int = 0):
        self.mm         = mm
        self.active_idx = active_idx

    @property
    def _active(self) -> Tuple[int, int, int, int]:
        return self.mm.monitors[self.active_idx]

    def to_screen(self, norm_x: float, norm_y: float) -> Tuple[int, int]:
        # Crop the outer ACTIVE_MARGIN from each camera edge so the user doesn't
        # need to reach the extreme camera corners — the usable center band maps
        # to the full screen.  Each monitor's own resolution is used automatically.
        span = 1.0 - 2 * ACTIVE_MARGIN
        ax = max(0.0, min(1.0, (norm_x - ACTIVE_MARGIN) / span))
        ay = max(0.0, min(1.0, (norm_y - ACTIVE_MARGIN) / span))
        mx, my, mw, mh = self._active
        return mx + int(ax * mw), my + int(ay * mh)

    def palm_center_screen(self, lm: np.ndarray) -> Tuple[int, int]:
        palm = (lm[0] + lm[9]) / 2.0
        return self.to_screen(palm[0], palm[1])


# ─────────────────────────────────────────────────────────────
#  SMOOTHER
# ─────────────────────────────────────────────────────────────

class Smoother:
    def __init__(self, alpha: float = SMOOTHING_ALPHA):
        self.alpha = alpha
        self._x: Optional[float] = None
        self._y: Optional[float] = None

    def update(self, x: float, y: float) -> Tuple[float, float]:
        if self._x is None:
            self._x, self._y = x, y
        else:
            self._x = self.alpha * x + (1 - self.alpha) * self._x
            self._y = self.alpha * y + (1 - self.alpha) * self._y
        return self._x, self._y

    def reset(self):
        self._x = self._y = None


# ─────────────────────────────────────────────────────────────
#  WIN32 OVERLAY  (native transparent window — thread-safe)
# ─────────────────────────────────────────────────────────────

class Win32Overlay(threading.Thread):
    """
    Transparent always-on-top Win32 window that draws hand cursors
    directly on the desktop across all monitors.

    Why Win32 instead of tkinter:
        tkinter must run on the main thread on Windows; mixing it with
        cv2.imshow (which also owns a Win32 message pump) in different
        threads freezes the entire desktop.  A native HWND created and
        pumped entirely inside this daemon thread has no such conflict.
    """

    _WM_UPDATE  = win32con.WM_USER + 1   # custom message: repaint now
    _CLASS_NAME = "GestureOverlay"
    _LWA_COLORKEY = 0x00000001

    def __init__(self, vx: int, vy: int, vw: int, vh: int):
        super().__init__(daemon=True)
        self.vx, self.vy, self.vw, self.vh = vx, vy, vw, vh
        self._hwnd: Optional[int] = None
        self._lock = threading.Lock()
        self._data = {
            'left': None, 'right': None,
            'state': AppState.IDLE, 'title': '',
            'close_pct': 0.0,
        }
        self.running   = True
        # Off-screen GDI buffer for double-buffering (allocated in _create_window)
        self._mem_dc:  Optional[int] = None
        self._mem_bmp: Optional[int] = None
        self._mem_old: Optional[int] = None

    # ── public API (called from main/camera thread) ─────────────

    def push(self, left, right, state, title='', close_pct=0.0,
             active_monitor=None, active_idx=0, n_monitors=1, switch_pct=0.0,
             scale_pct=0.0, scale_hand_pos=None):
        """Update cursor data and trigger a repaint (thread-safe)."""
        with self._lock:
            self._data = {
                'left': left, 'right': right,
                'state': state, 'title': title,
                'close_pct': close_pct,
                'scale_pct': scale_pct,
                'scale_hand_pos': scale_hand_pos,
                'active_monitor': active_monitor,
                'active_idx': active_idx,
                'n_monitors': n_monitors,
                'switch_pct': switch_pct,
            }
        if self._hwnd:
            try:
                win32gui.PostMessage(self._hwnd, self._WM_UPDATE, 0, 0)
            except Exception:
                pass

    def stop(self):
        self.running = False
        if self._hwnd:
            try:
                win32gui.PostMessage(self._hwnd, win32con.WM_CLOSE, 0, 0)
            except Exception:
                pass

    # ── thread body ─────────────────────────────────────────────

    def run(self):
        try:
            self._hwnd = self._create_window()
            win32gui.PumpMessages()
        except Exception as e:
            print(f"[Overlay] Window creation failed: {e}")
        finally:
            try:
                win32gui.UnregisterClass(self._CLASS_NAME,
                                         win32api.GetModuleHandle(None))
            except Exception:
                pass

    def _create_window(self) -> int:
        wc = win32gui.WNDCLASS()
        wc.hInstance     = win32api.GetModuleHandle(None)
        wc.lpszClassName = self._CLASS_NAME
        wc.hbrBackground = win32gui.CreateSolidBrush(_COL_CHROMA)
        wc.lpfnWndProc   = {
            win32con.WM_PAINT:       self._on_paint,
            win32con.WM_DESTROY:     self._on_destroy,
            win32con.WM_ERASEBKGND:  self._on_erase,
            self._WM_UPDATE:         self._on_update,
        }
        try:
            win32gui.RegisterClass(wc)
        except Exception:
            pass  # already registered from a previous run

        hwnd = win32gui.CreateWindowEx(
            win32con.WS_EX_LAYERED    |   # enables transparency
            win32con.WS_EX_TOPMOST    |   # always on top
            win32con.WS_EX_TRANSPARENT|   # mouse clicks pass through
            win32con.WS_EX_NOACTIVATE |   # never steals focus
            win32con.WS_EX_TOOLWINDOW,    # hidden from taskbar/Alt-Tab
            self._CLASS_NAME,
            "GestureOverlay",
            win32con.WS_POPUP,
            self.vx, self.vy, self.vw, self.vh,
            None, None,
            wc.hInstance, None
        )

        # Color-key transparency: any pixel of _COL_CHROMA becomes invisible
        win32gui.SetLayeredWindowAttributes(
            hwnd, _COL_CHROMA, 0, self._LWA_COLORKEY
        )
        win32gui.ShowWindow(hwnd, win32con.SW_SHOWNOACTIVATE)
        win32gui.UpdateWindow(hwnd)

        # Allocate a persistent off-screen buffer for double-buffering.
        # Re-using the same bitmap every frame avoids repeated alloc/free at 20fps
        # and the single BitBlt to the real DC is atomic from DWM's perspective.
        _hdc = win32gui.GetDC(hwnd)
        self._mem_dc  = win32gui.CreateCompatibleDC(_hdc)
        self._mem_bmp = win32gui.CreateCompatibleBitmap(_hdc, self.vw, self.vh)
        self._mem_old = win32gui.SelectObject(self._mem_dc, self._mem_bmp)
        win32gui.ReleaseDC(hwnd, _hdc)

        return hwnd

    # ── message handlers (all run in THIS thread via PumpMessages) ──

    def _on_update(self, hwnd, msg, wp, lp):
        # bErase=False: we fill the background ourselves in _render (via mem_dc),
        # so we don't need Windows to pre-fill with the background brush.
        win32gui.InvalidateRect(hwnd, None, False)
        return 0

    def _on_erase(self, hwnd, msg, wp, lp):
        # Returning non-zero tells Windows we handled the erase — suppresses the
        # default hbrBackground fill that would make the whole overlay flash transparent
        # between WM_ERASEBKGND and WM_PAINT.
        return 1

    def _on_destroy(self, hwnd, msg, wp, lp):
        # Release the off-screen GDI buffer before the DC becomes invalid
        if self._mem_dc:
            if self._mem_old:
                win32gui.SelectObject(self._mem_dc, self._mem_old)
            if self._mem_bmp:
                win32gui.DeleteObject(self._mem_bmp)
            win32gui.DeleteDC(self._mem_dc)
            self._mem_dc = self._mem_bmp = self._mem_old = None
        win32gui.PostQuitMessage(0)
        return 0

    def _on_paint(self, hwnd, msg, wp, lp):
        hdc, ps = win32gui.BeginPaint(hwnd)
        try:
            if self._mem_dc:
                # Render everything to the off-screen buffer, then blit in one
                # atomic operation.  DWM never sees a half-drawn frame → no tearing.
                self._render(self._mem_dc)
                win32gui.BitBlt(
                    hdc, 0, 0, self.vw, self.vh,
                    self._mem_dc, 0, 0, win32con.SRCCOPY,
                )
            else:
                self._render(hdc)   # fallback if buffer wasn't allocated
        finally:
            win32gui.EndPaint(hwnd, ps)
        return 0

    def _render(self, hdc):
        with self._lock:
            d = dict(self._data)

        # Fill entire canvas with the chroma-key color (= transparent)
        bg = win32gui.CreateSolidBrush(_COL_CHROMA)
        win32gui.FillRect(hdc, (0, 0, self.vw, self.vh), bg)
        win32gui.DeleteObject(bg)

        state          = d['state']
        title          = d['title']
        close_pct      = d['close_pct']
        switch_pct     = d.get('switch_pct', 0.0)
        active_monitor = d.get('active_monitor')
        active_idx     = d.get('active_idx', 0)
        n_monitors     = d.get('n_monitors', 1)

        # ── Active-monitor badge (top-center of the controlled monitor) ──────
        if active_monitor and n_monitors > 1:
            mx, my, mw, mh = active_monitor
            bx = mx - self.vx + mw // 2  # center-x of active monitor in canvas coords
            by = my - self.vy + 8         # near top edge

            # Background pill
            pill_w, pill_h = 110, 22
            bg2 = win32gui.CreateSolidBrush(_rgb(20, 20, 40))
            win32gui.FillRect(hdc, (bx - pill_w//2, by, bx + pill_w//2, by + pill_h), bg2)
            win32gui.DeleteObject(bg2)

            # Dot row: one dot per monitor, filled = active
            dot_gap = 14
            total_dots_w = n_monitors * dot_gap
            dot_x0 = bx - total_dots_w // 2 + dot_gap // 2
            for i in range(n_monitors):
                dx = dot_x0 + i * dot_gap
                dy2 = by + pill_h // 2
                if i == active_idx:
                    self._draw_filled_circle(hdc, dx, dy2, 5, _COL_CYAN)
                else:
                    self._draw_filled_circle(hdc, dx, dy2, 3, _COL_GRAY)

            # "MON N" label to the right of the dots
            self._draw_text(hdc, f"MON {active_idx + 1}",
                            bx + total_dots_w // 2 - 4, by,
                            bx + total_dots_w // 2 + 44, by + pill_h,
                            _COL_CYAN)

        # ── Switch-monitor progress arc (while holding open palm) ─────────────
        if switch_pct > 0 and active_monitor:
            mx, my, mw, mh = active_monitor
            ax = mx - self.vx + 38
            ay = my - self.vy + 38
            r2 = 26
            ring_bg = win32gui.CreateSolidBrush(_rgb(30, 30, 50))
            win32gui.Ellipse(hdc, ax - r2, ay - r2, ax + r2, ay + r2)
            win32gui.DeleteObject(ring_bg)
            # Progress wedge approximated as a filled arc sector using FillRect strips
            # (simpler than Arc since win32gui doesn't wrap Pie directly)
            # Just show a growing ring border via concentric circles
            filled = win32gui.CreateSolidBrush(_COL_CYAN)
            # Draw a thin arc by filling an annulus sector approximation:
            # use the close-progress bar style instead (horizontal bar below badge)
            win32gui.DeleteObject(filled)
            # Horizontal bar below the badge
            bx2 = mx - self.vx + mw // 2
            by2 = my - self.vy + 34
            bar_half = 55
            bw2 = int(switch_pct * bar_half * 2)
            bg3 = win32gui.CreateSolidBrush(_rgb(40, 40, 60))
            win32gui.FillRect(hdc, (bx2 - bar_half, by2, bx2 + bar_half, by2 + 8), bg3)
            win32gui.DeleteObject(bg3)
            fg2 = win32gui.CreateSolidBrush(_COL_CYAN)
            win32gui.FillRect(hdc, (bx2 - bar_half, by2, bx2 - bar_half + bw2, by2 + 8), fg2)
            win32gui.DeleteObject(fg2)
            self._draw_text(hdc, "SWITCH MON",
                            bx2 - bar_half, by2 + 10,
                            bx2 + bar_half, by2 + 26,
                            _COL_CYAN)

        ring_col = {
            AppState.IDLE:  _COL_GRAY,
            AppState.GRAB:  _COL_WHITE,
            AppState.SCALE: _COL_CYAN,
        }[state]

        hand_specs = [
            (d['left'],  _COL_GREEN, 'L'),
            (d['right'], _COL_BLUE,  'R'),
        ]

        drawn: List[Tuple[int, int]] = []
        for pos, dot_col, label in hand_specs:
            if pos is None:
                continue
            sx = pos[0] - self.vx
            sy = pos[1] - self.vy
            rc = ring_col if state != AppState.IDLE else dot_col
            r  = 20 if state != AppState.IDLE else 14

            self._draw_cursor(hdc, sx, sy, r, rc, dot_col)
            drawn.append((sx, sy))

        # Scale line between both hands
        if state == AppState.SCALE and len(drawn) == 2:
            lx, ly = drawn[0]
            rx, ry = drawn[1]
            self._draw_line(hdc, lx, ly, rx, ry, _COL_CYAN, thickness=2)
            mx2, my2 = (lx + rx) // 2, (ly + ry) // 2
            self._draw_filled_circle(hdc, mx2, my2, 5, _COL_CYAN)

        # Window title tooltip above first cursor
        if title and drawn:
            tx, ty = drawn[0]
            r_first = 20 if state != AppState.IDLE else 14
            self._draw_text(hdc, title[:55],
                            tx - 180, ty - r_first - 32,
                            tx + 180, ty - r_first - 10,
                            _COL_WHITE)

        scale_pct      = d.get('scale_pct', 0.0)
        scale_hand_pos = d.get('scale_hand_pos')

        # Scale-progress bar below the opening (other) hand
        if scale_pct > 0 and scale_hand_pos:
            tx, ty = scale_hand_pos
            bw = int(scale_pct * 140)
            bg = win32gui.CreateSolidBrush(_rgb(60, 60, 60))
            win32gui.FillRect(hdc, (tx - 70, ty + 26, tx + 70, ty + 42), bg)
            win32gui.DeleteObject(bg)
            fg = win32gui.CreateSolidBrush(_COL_CYAN)
            win32gui.FillRect(hdc, (tx - 70, ty + 26, tx - 70 + bw, ty + 42), fg)
            win32gui.DeleteObject(fg)
            self._draw_text(hdc, "SCALE",
                            tx - 70, ty + 26, tx + 70, ty + 42,
                            _COL_WHITE)

        # Close-progress bar below first cursor
        if close_pct > 0 and drawn:
            tx, ty = drawn[0]
            bw = int(close_pct * 140)
            bg2 = win32gui.CreateSolidBrush(_rgb(60, 60, 60))
            win32gui.FillRect(hdc, (tx - 70, ty + 26, tx + 70, ty + 42), bg2)
            win32gui.DeleteObject(bg2)
            fg = win32gui.CreateSolidBrush(_COL_RED)
            win32gui.FillRect(hdc, (tx - 70, ty + 26, tx - 70 + bw, ty + 42), fg)
            win32gui.DeleteObject(fg)
            self._draw_text(hdc, "CLOSE",
                            tx - 70, ty + 26, tx + 70, ty + 42,
                            _COL_WHITE)

    # ── GDI drawing helpers ─────────────────────────────────────

    def _draw_cursor(self, hdc, sx, sy, r, ring_col, dot_col):
        null_brush = win32gui.GetStockObject(win32con.NULL_BRUSH)
        old_brush  = win32gui.SelectObject(hdc, null_brush)

        # Outer ring (outline only; interior stays transparent via null brush)
        ring_pen = win32gui.CreatePen(win32con.PS_SOLID, 2, ring_col)
        old_pen  = win32gui.SelectObject(hdc, ring_pen)
        win32gui.Ellipse(hdc, sx - r, sy - r, sx + r, sy + r)
        win32gui.SelectObject(hdc, old_pen)
        win32gui.DeleteObject(ring_pen)

        win32gui.SelectObject(hdc, old_brush)

        # Crosshair (thin FillRect segments — avoids MoveToEx/LineTo portability issues)
        gap = 5
        ch  = win32gui.CreateSolidBrush(ring_col)
        win32gui.FillRect(hdc, (sx - r - 7, sy - 1, sx - gap,    sy + 2), ch)
        win32gui.FillRect(hdc, (sx + gap,   sy - 1, sx + r + 7,  sy + 2), ch)
        win32gui.FillRect(hdc, (sx - 1, sy - r - 7, sx + 2, sy - gap   ), ch)
        win32gui.FillRect(hdc, (sx - 1, sy + gap,   sx + 2, sy + r + 7 ), ch)
        win32gui.DeleteObject(ch)

        # Center filled dot
        self._draw_filled_circle(hdc, sx, sy, 5, dot_col)

    def _draw_filled_circle(self, hdc, cx, cy, r, col):
        b = win32gui.CreateSolidBrush(col)
        p = win32gui.CreatePen(win32con.PS_SOLID, 1, col)
        old_b = win32gui.SelectObject(hdc, b)
        old_p = win32gui.SelectObject(hdc, p)
        win32gui.Ellipse(hdc, cx - r, cy - r, cx + r, cy + r)
        win32gui.SelectObject(hdc, old_b)
        win32gui.SelectObject(hdc, old_p)
        win32gui.DeleteObject(b)
        win32gui.DeleteObject(p)

    def _draw_line(self, hdc, x1, y1, x2, y2, col, thickness=1):
        # Polyline draws a true diagonal; the old FillRect hack only worked for
        # near-horizontal or near-vertical lines and produced wrong results otherwise.
        pen     = win32gui.CreatePen(win32con.PS_SOLID, thickness, col)
        old_pen = win32gui.SelectObject(hdc, pen)
        old_br  = win32gui.SelectObject(hdc, win32gui.GetStockObject(win32con.NULL_BRUSH))
        win32gui.Polyline(hdc, [(x1, y1), (x2, y2)])
        win32gui.SelectObject(hdc, old_pen)
        win32gui.SelectObject(hdc, old_br)
        win32gui.DeleteObject(pen)

    def _draw_text(self, hdc, text, l, t, r, b, col):
        win32gui.SetTextColor(hdc, col)
        win32gui.SetBkMode(hdc, win32con.TRANSPARENT)
        win32gui.DrawText(
            hdc, text, -1, (l, t, r, b),
            win32con.DT_CENTER | win32con.DT_SINGLELINE | win32con.DT_VCENTER
        )


# ─────────────────────────────────────────────────────────────
#  CAMERA STREAM  (daemon thread — keeps latest frame ready)
# ─────────────────────────────────────────────────────────────

class CameraStream(threading.Thread):
    """
    Runs cap.read() in a daemon thread so the main loop never blocks
    waiting for the camera to deliver a frame.  The main thread always
    gets the most-recently-captured frame with zero wait.
    """

    def __init__(self, src: int, width: int, height: int, fps: int):
        super().__init__(daemon=True)

        MJPG = cv2.VideoWriter_fourcc(*'MJPG')

        # Try backends in order: DirectShow → MSMF → auto.
        # DirectShow has lower latency when it works, but some camera drivers on
        # Windows 11 deliver <2 fps with it.  MSMF is the modern Windows capture
        # stack and handles those cameras correctly.
        cap = None
        for backend, backend_name in [
            (cv2.CAP_DSHOW, "DirectShow"),
            (cv2.CAP_MSMF,  "MSMF"),
            (cv2.CAP_ANY,   "auto"),
        ]:
            c = cv2.VideoCapture(src, backend)
            if not c.isOpened():
                c.release()
                continue

            c.set(cv2.CAP_PROP_FOURCC, MJPG)
            c.set(cv2.CAP_PROP_FRAME_WIDTH,  width)
            c.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
            c.set(cv2.CAP_PROP_FPS, fps)
            c.set(cv2.CAP_PROP_BUFFERSIZE, 1)

            actual_fourcc = int(c.get(cv2.CAP_PROP_FOURCC))
            if actual_fourcc != MJPG:
                # MJPG rejected — drop to 640×480 so YUY2 stays within USB 2.0 bandwidth.
                c.set(cv2.CAP_PROP_FRAME_WIDTH,  640)
                c.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
                print(f"  [camera] {backend_name}: MJPG not accepted — fell back to 640x480 YUY2")

            # Drain 3 warmup frames and time them to detect a slow/stuck backend.
            t0 = time.time()
            frames_ok = sum(1 for _ in range(3) if c.read()[0])
            elapsed = time.time() - t0

            if frames_ok >= 2 and elapsed < 1.5:
                print(f"  [camera] Backend: {backend_name}  ({elapsed/max(frames_ok,1)*1000:.0f} ms/frame during warmup)")
                cap = c
                break

            print(f"  [camera] {backend_name} too slow ({elapsed:.1f}s for 3 frames) — trying next backend")
            c.release()

        if cap is None:
            raise RuntimeError("Camera did not open with any backend. Check WEBCAM_INDEX.")

        self.cap = cap

        for _ in range(3):   # drain remaining warm-up frames
            self.cap.read()
        ok, frame = self.cap.read()
        if not ok:
            raise RuntimeError("Camera opened but could not read a frame.")

        self.frame   = frame.copy()
        self.seq     = 0
        self.stopped = False

    def run(self):
        while not self.stopped:
            ok, frame = self.cap.read()
            if ok:
                self.frame = frame.copy()  # deep copy before releasing DirectShow buffer
                self.seq  += 1
        self.cap.release()

    def stop(self):
        self.stopped = True


# ─────────────────────────────────────────────────────────────
#  INFERENCE STREAM  (daemon thread — decouples MediaPipe from display)
# ─────────────────────────────────────────────────────────────

class InferenceStream(threading.Thread):
    """
    Runs MediaPipe hands.process() in a dedicated daemon thread so the
    main display loop is never blocked waiting for inference.

    Why this fixes the FPS cap:
        hands.process() on CPU takes 50–120 ms.  When it lived in the
        main thread it capped the entire pipeline — display, FSM, and
        overlay — to inference speed (~8–12 fps).  Moving it here lets
        the main thread redraw the camera preview at the full camera rate
        (~30 fps) while gestures update as fast as the CPU allows.

    Pattern mirrors CameraStream: result + seq let the main thread skip
    work when nothing new has arrived, without spinning at 100% CPU.

    Fix 1.2 is also applied here: inference runs on 320×180 instead of
    480×270 (44% fewer pixels, same landmark accuracy since MediaPipe
    outputs normalized 0–1 coordinates regardless of input resolution).
    """

    def __init__(self, cam: 'CameraStream'):
        super().__init__(daemon=True)
        self.cam   = cam
        self.hands = mp.solutions.hands.Hands(
            model_complexity=0,
            max_num_hands=2,
            min_detection_confidence=0.7,
            min_tracking_confidence=0.5,
        )
        self.result  = None   # latest mp Hands result object
        self.seq     = 0      # monotonic counter; increments on each new result
        self._lock   = threading.Lock()
        self.stopped = False

    def run(self):
        last_cam_seq = -1
        while not self.stopped:
            if self.cam.seq == last_cam_seq:
                time.sleep(0.001)   # yield CPU while waiting for next camera frame
                continue
            last_cam_seq = self.cam.seq

            frame = self.cam.frame                          # already deep-copied by CameraStream
            frame = cv2.flip(frame, 1)                      # mirror before inference (same as before)
            small = cv2.resize(frame, (320, 180))           # Fix 1.2: 44% fewer pixels than 480×270
            rgb   = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
            rgb.flags.writeable = False
            result = self.hands.process(rgb)
            rgb.flags.writeable = True

            with self._lock:
                self.result = result
                self.seq   += 1

    def get(self) -> Tuple[object, int]:
        """Return (result, seq) — thread-safe, non-blocking."""
        with self._lock:
            return self.result, self.seq

    def stop(self):
        self.stopped = True
        self.hands.close()


# ─────────────────────────────────────────────────────────────
#  MAIN APPLICATION
# ─────────────────────────────────────────────────────────────

class GestureWindowController:

    def __init__(self, camera_idx: int = WEBCAM_INDEX):
        self.camera_idx  = camera_idx
        self.monitor_mgr = MonitorManager()

        self.mp_hands   = mp.solutions.hands
        self.mp_drawing = mp.solutions.drawing_utils
        # NOTE: mp Hands instance lives in InferenceStream now, not here.

        self.state       = AppState.IDLE
        self.left_hand   = HandState()
        self.right_hand  = HandState()
        self.window_snap = WindowSnapshot()

        self.active_monitor_idx = 0

        self.detector       = GestureDetector()
        self.win_manager    = WindowManager()
        self.mapper         = ScreenMapper(self.monitor_mgr, self.active_monitor_idx)
        self.left_smoother  = Smoother()
        self.right_smoother = Smoother()
        self.span_smoother  = Smoother(alpha=0.4)

        mm = self.monitor_mgr
        self.overlay = Win32Overlay(
            mm.virtual_left, mm.virtual_top,
            mm.virtual_width, mm.virtual_height,
        )

        self.cam       = None
        self.inference = None   # InferenceStream — started in run()
        self.running   = False
        self.fps     = 0.0
        self._fps_t  = time.time()
        self._fps_f  = 0

        self._action_cooldown  = 0
        self._both_one_frames  = 0   # shared: frames where BOTH hands simultaneously show ☝️
        self._last_overlay_push = 0.0

        # BGR colors for cv2 HUD
        self.COL_IDLE  = (180, 180, 180)
        self.COL_GRAB  = (50, 200, 50)
        self.COL_SCALE = (50, 150, 255)
        self.COL_WHITE = (255, 255, 255)
        self.COL_BLACK = (0, 0, 0)
        self.COL_RED   = (50, 50, 255)

    # ── MAIN LOOP ──────────────────────────────────────────────

    def run(self):
        # Fix 1.3 — set Windows multimedia timer to 1 ms resolution so that
        # waitKey(1) actually waits ~1 ms instead of the default ~15 ms tick.
        try:
            ctypes.windll.winmm.timeBeginPeriod(1)
        except Exception:
            pass

        self.overlay.start()

        # Show loading screen while camera and inference thread open
        _WIN = "Gesture Controller  [Q = quit]"
        _DW, _DH = 960, 540   # display resolution; HUD is drawn here
        cv2.namedWindow(_WIN, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(_WIN, _DW, _DH)
        try:
            cv2.setWindowProperty(_WIN, cv2.WND_PROP_ASPECT_RATIO, cv2.WINDOW_KEEPRATIO)
        except Exception:
            pass
        loading = np.zeros((_DH, _DW, 3), dtype=np.uint8)
        cv2.putText(loading, "Initializing camera...",
                    (220, 270), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (200, 200, 200), 2)
        cv2.imshow(_WIN, loading)
        cv2.waitKey(1)

        # Camera thread — never blocks main loop on cap.read()
        self.cam = CameraStream(self.camera_idx, FRAME_WIDTH, FRAME_HEIGHT, 30)
        self.cam.start()

        # Inference thread — never blocks main loop on hands.process()
        # Display runs at full camera FPS; gestures run at inference FPS.
        self.inference = InferenceStream(self.cam)
        self.inference.start()

        self.running = True

        # ── Startup diagnostics — printed once to the console ───────────────────
        fourcc_int  = int(self.cam.cap.get(cv2.CAP_PROP_FOURCC))
        fourcc_str  = "".join(chr((fourcc_int >> (8 * i)) & 0xFF) for i in range(4))
        cam_fps_neg = self.cam.cap.get(cv2.CAP_PROP_FPS)
        cam_w       = int(self.cam.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        cam_h       = int(self.cam.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        print("\n" + "="*58)
        print("  DIAGNOSTICS")
        print(f"  Negotiated camera: {cam_w}×{cam_h}  {fourcc_str}  {cam_fps_neg:.0f} fps")
        print(f"  MJPG active      : {'✓ YES' if fourcc_str == 'MJPG' else '✗ NO — fell back to ' + fourcc_str}")
        print(f"  timeBeginPeriod  : active (1 ms)")
        print(f"  Inference res.   : 320×180  (model_complexity=0)")
        print("  On-screen FPS:")
        print("    cam_fps   → real CameraStream speed")
        print("    inf_fps   → real InferenceStream speed")
        print("    disp_fps  → what is shown in the HUD")
        print("="*58 + "\n")
        print("  Press Q or ESC in the camera window to quit.")
        print()

        # Separate per-thread FPS counters printed every 3s
        _diag_cam_seq0   = self.cam.seq
        _diag_inf_seq0   = 0
        _diag_t0         = time.time()
        _DIAG_INTERVAL   = 3.0

        _last_cam_seq = -1   # last camera frame shown
        _last_inf_seq = -1   # last inference result processed
        lm_to_draw    = []   # persists between inference updates so display stays annotated

        while self.running:
            # ── keyboard input ───────────────────────────────────────────────────
            key = cv2.waitKey(1) & 0xFF
            if key in (ord('q'), ord('Q'), 27):
                break
            elif key in (ord('1'), ord('2'), ord('3')):
                idx = int(chr(key)) - 1
                if idx < len(self.monitor_mgr.monitors):
                    self._switch_monitor(idx)

            inf_result, inf_seq = self.inference.get()
            cam_seq             = self.cam.seq

            # Nothing new from either source — tight-loop would burn CPU for no gain
            if cam_seq == _last_cam_seq and inf_seq == _last_inf_seq:
                continue

            # ── New inference result → update gesture state ──────────────────────
            if inf_seq != _last_inf_seq:
                _last_inf_seq = inf_seq

                # Don't pre-clear landmarks — _hand_lost() applies a grace window
                # before clearing, so 1-2 frame drops don't break a grab in progress.
                lm_to_draw     = []
                detected_left  = False
                detected_right = False

                if inf_result and inf_result.multi_hand_landmarks and inf_result.multi_handedness:
                    for hand_lms, handedness in zip(
                        inf_result.multi_hand_landmarks, inf_result.multi_handedness
                    ):
                        label = handedness.classification[0].label
                        lm = np.array([[p.x, p.y, p.z] for p in hand_lms.landmark])
                        # MediaPipe mirrors L/R relative to person — swap back
                        if label == "Right":
                            self._update_hand(self.left_hand, lm, self.left_smoother)
                            lm_to_draw.append((hand_lms, (100, 255, 100)))
                            detected_left = True
                        else:
                            self._update_hand(self.right_hand, lm, self.right_smoother)
                            lm_to_draw.append((hand_lms, (100, 150, 255)))
                            detected_right = True

                if not detected_left:
                    self._hand_lost(self.left_hand)
                if not detected_right:
                    self._hand_lost(self.right_hand)

                if self._action_cooldown > 0:
                    self._action_cooldown -= 1
                else:
                    self._update_state()

                self._push_overlay()

                # ── Per-thread FPS diagnostics (printed every 3 s) ──────────────
                _elapsed_diag = time.time() - _diag_t0
                if _elapsed_diag >= _DIAG_INTERVAL:
                    _cam_fps_real = (self.cam.seq - _diag_cam_seq0) / _elapsed_diag
                    _inf_fps_real = (inf_seq       - _diag_inf_seq0) / _elapsed_diag
                    print(f"  [diag]  cam={_cam_fps_real:.1f}fps  "
                          f"inf={_inf_fps_real:.1f}fps  "
                          f"disp={self.fps:.1f}fps")
                    _diag_cam_seq0 = self.cam.seq
                    _diag_inf_seq0 = inf_seq
                    _diag_t0       = time.time()

            # ── New camera frame → redraw display ───────────────────────────────
            # lm_to_draw may be from the previous inference result — that is fine;
            # at 30 fps the difference is imperceptible.
            if cam_seq != _last_cam_seq:
                _last_cam_seq = cam_seq

                frame = self.cam.frame          # deep-copied by CameraStream
                frame = cv2.flip(frame, 1)      # mirror for natural preview

                self._fps_f += 1
                elapsed = time.time() - self._fps_t
                if elapsed >= 1.0:
                    self.fps = self._fps_f / elapsed
                    self._fps_f = 0
                    self._fps_t = time.time()

                # Landmarks + HUD drawn on display frame (960×540)
                display = cv2.resize(frame, (_DW, _DH))
                if DEBUG_OVERLAY:
                    for hand_lms, color in lm_to_draw:
                        self.mp_drawing.draw_landmarks(
                            display, hand_lms, self.mp_hands.HAND_CONNECTIONS,
                            self.mp_drawing.DrawingSpec(color, 2, 4),
                            self.mp_drawing.DrawingSpec((200, 200, 200), 1),
                        )
                    self._draw_hud(display, _DW, _DH)

                # Letterbox to preserve 16:9 when user resizes the window freely
                try:
                    _, _, _ww, _wh = cv2.getWindowImageRect(_WIN)
                    _ww = _ww if _ww > 0 else _DW
                    _wh = _wh if _wh > 0 else _DH
                except Exception:
                    _ww, _wh = _DW, _DH
                _tr = _DW / _DH
                if abs(_ww / _wh - _tr) < 0.02:
                    cv2.imshow(_WIN, display)
                else:
                    if _ww / _wh > _tr:
                        _fh, _fw = _wh, int(_wh * _tr)
                    else:
                        _fw, _fh = _ww, int(_ww / _tr)
                    _cnv = np.zeros((_wh, _ww, 3), dtype=np.uint8)
                    _x0, _y0 = (_ww - _fw) // 2, (_wh - _fh) // 2
                    _cnv[_y0:_y0 + _fh, _x0:_x0 + _fw] = cv2.resize(display, (_fw, _fh))
                    cv2.imshow(_WIN, _cnv)

        self._cleanup()

    # ── HAND STATE UPDATE ──────────────────────────────────────

    def _update_hand(self, hand: HandState, lm: np.ndarray, smoother: Smoother):
        hand.landmarks   = lm
        hand.detect_miss = 0   # hand is visible this frame
        hand.miss_frames = 0   # reset within-gesture miss counter on (re)detection
        sx, sy = self.mapper.palm_center_screen(lm)
        hand.smooth_x, hand.smooth_y = smoother.update(sx, sy)
        hand.initialized = True

        raw_fist, raw_open, raw_one, raw_two, raw_three = self.detector.classify(lm)

        # Mutually exclusive debounce: increment the active gesture, zero the rest
        if raw_fist:
            hand.fist_frames += 1
            hand.miss_frames = 0
            hand.open_frames = hand.one_frames = hand.two_frames = hand.three_frames = 0
        elif raw_open:
            hand.open_frames += 1
            hand.miss_frames = 0
            hand.fist_frames = hand.one_frames = hand.two_frames = hand.three_frames = 0
        elif raw_one:
            hand.one_frames += 1
            hand.miss_frames = 0
            hand.fist_frames = hand.open_frames = hand.two_frames = hand.three_frames = 0
        elif raw_two:
            hand.two_frames += 1
            hand.miss_frames = 0
            hand.fist_frames = hand.open_frames = hand.one_frames = hand.three_frames = 0
        elif raw_three:
            hand.three_frames += 1
            hand.miss_frames = 0
            hand.fist_frames = hand.open_frames = hand.one_frames = hand.two_frames = 0
        else:
            # 3-frame grace window: brief detection flicker doesn't pull counters down
            hand.miss_frames += 1
            if hand.miss_frames >= 3:
                hand.fist_frames  = max(0, hand.fist_frames  - 1)
                hand.open_frames  = max(0, hand.open_frames  - 1)
                hand.one_frames   = max(0, hand.one_frames   - 1)
                hand.two_frames   = max(0, hand.two_frames   - 1)
                hand.three_frames = max(0, hand.three_frames - 1)

        hand.is_fist  = hand.fist_frames  >= GESTURE_HOLD_FRAMES
        hand.is_open  = hand.open_frames  >= GESTURE_HOLD_FRAMES
        hand.is_one   = hand.one_frames   >= GESTURE_HOLD_FRAMES
        hand.is_two   = hand.two_frames   >= GESTURE_HOLD_FRAMES
        hand.is_three = hand.three_frames >= GESTURE_HOLD_FRAMES

    def _hand_lost(self, hand: HandState):
        """Called each inference frame when MediaPipe did not detect this hand.

        Keeps hand.landmarks (and smooth_x/y) alive for HAND_DETECT_GRACE frames so
        that brief 1-2 frame drops from the tracker don't interrupt a grab or scale
        operation.  Once the grace window expires, landmarks are cleared and gesture
        counters decay — matching the behaviour of a deliberate hand release.
        """
        hand.detect_miss += 1
        if hand.detect_miss >= HAND_DETECT_GRACE:
            hand.landmarks    = None
            hand.fist_frames  = max(0, hand.fist_frames  - 1)
            hand.open_frames  = max(0, hand.open_frames  - 1)
            hand.one_frames   = max(0, hand.one_frames   - 1)
            hand.two_frames   = max(0, hand.two_frames   - 1)
            hand.three_frames = max(0, hand.three_frames - 1)
            hand.is_fist  = hand.fist_frames  >= GESTURE_HOLD_FRAMES
            hand.is_open  = hand.open_frames  >= GESTURE_HOLD_FRAMES
            hand.is_one   = hand.one_frames   >= GESTURE_HOLD_FRAMES
            hand.is_two   = hand.two_frames   >= GESTURE_HOLD_FRAMES
            hand.is_three = hand.three_frames >= GESTURE_HOLD_FRAMES

    # ── GESTURE STATE MACHINE ──────────────────────────────────

    def _update_state(self):
        L, R = self.left_hand, self.right_hand
        Lv = L.landmarks is not None
        Rv = R.landmarks is not None

        if self.state == AppState.IDLE:
            # Both hands ☝️ held simultaneously for 1.5s → cycle to next monitor
            if (Lv and Rv
                    and L.one_frames >= GESTURE_HOLD_FRAMES
                    and R.one_frames >= GESTURE_HOLD_FRAMES):
                self._both_one_frames += 1
                if self._both_one_frames >= MONITOR_TARGET_FRAMES:
                    self._both_one_frames = 0
                    self._switch_monitor((self.active_monitor_idx + 1) % len(self.monitor_mgr.monitors))
                    return
            else:
                self._both_one_frames = 0

            # ✌️ over a window → minimize it (checked before fist so gestures don't conflict)
            if Lv and L.is_two:
                self._minimize_under(L)
            elif Rv and R.is_two:
                self._minimize_under(R)
            elif Lv and L.is_fist and not (Rv and R.is_fist):
                self._start_grab(L, 'L')
            elif Rv and R.is_fist and not (Lv and L.is_fist):
                self._start_grab(R, 'R')
            elif Lv and Rv and L.is_fist and R.is_fist:
                self._start_grab(R, 'R')

        elif self.state == AppState.GRAB:
            snap   = self.window_snap
            active = L if snap.grab_side == 'L' else R
            other  = R if snap.grab_side == 'L' else L
            active_vis = Lv if snap.grab_side == 'L' else Rv
            other_vis  = Rv if snap.grab_side == 'L' else Lv

            if not active_vis or not active.is_fist:
                self._stop(); return

            if other_vis and other.is_two:
                self._move_to_next_monitor()
                self._stop(); return

            if other_vis and other.is_fist:
                snap.both_fist_frames += 1
                if snap.both_fist_frames >= CLOSE_HOLD_FRAMES:
                    self._close_grabbed(); return
            else:
                snap.both_fist_frames = 0

            if other_vis and other.is_open:
                snap.scale_hold_frames += 1
                if snap.scale_hold_frames >= SCALE_HOLD_FRAMES:
                    self._start_scale(); return
            else:
                snap.scale_hold_frames = 0

            dx = active.smooth_x - snap.origin_hand_x
            dy = active.smooth_y - snap.origin_hand_y
            if abs(dx) > MOVE_DEADZONE_PX or abs(dy) > MOVE_DEADZONE_PX:
                self.win_manager.move_window(
                    snap.hwnd,
                    snap.origin_win_x + dx, snap.origin_win_y + dy,
                    snap.width, snap.height,
                )

        elif self.state == AppState.SCALE:
            if not Lv or not Rv:
                self._stop(); return
            if L.is_open and R.is_open:
                self._stop(); return

            span = float(np.hypot(L.smooth_x - R.smooth_x, L.smooth_y - R.smooth_y))
            span, _ = self.span_smoother.update(span, span)

            snap = self.window_snap
            if snap.base_span < 1:
                snap.base_span   = span
                snap.base_width  = snap.width
                snap.base_height = snap.height
                return

            scale = (span / snap.base_span - 1.0) * SCALE_SENSITIVITY + 1.0
            scale = max(0.3, min(scale, 4.0))
            new_w = int(snap.base_width  * scale)
            new_h = int(snap.base_height * scale)
            mid_x = (L.smooth_x + R.smooth_x) / 2
            mid_y = (L.smooth_y + R.smooth_y) / 2
            self.win_manager.move_window(
                snap.hwnd,
                int(mid_x - new_w / 2), int(mid_y - new_h / 2),
                new_w, new_h,
            )

    # ── STATE TRANSITIONS ──────────────────────────────────────

    def _start_grab(self, hand: HandState, side: str):
        result = self.win_manager.find_window_at(
            int(hand.smooth_x), int(hand.smooth_y)
        )
        if not result:
            return
        hwnd, title, rect = result
        l, t, r, b = rect
        self.window_snap = WindowSnapshot(
            hwnd=hwnd, title=title,
            origin_win_x=l, origin_win_y=t,
            origin_hand_x=hand.smooth_x, origin_hand_y=hand.smooth_y,
            width=r - l, height=b - t,
            grab_side=side,
        )
        self.win_manager.bring_to_front(hwnd)
        self.state = AppState.GRAB
        print(f"GRAB   -> '{title}'  [{l},{t}]  {r-l}x{b-t}px")

    def _minimize_under(self, hand: HandState):
        """✌️ in IDLE state: minimize whichever window is under the cursor."""
        result = self.win_manager.find_window_at(
            int(hand.smooth_x), int(hand.smooth_y)
        )
        if not result:
            return
        hwnd, title, _ = result
        self.win_manager.minimize_window(hwnd)
        print(f"MINIMIZE -> '{title}'")
        self._action_cooldown = 20   # ~0.7s grace so the gesture doesn't fire twice

    def _start_scale(self):
        try:
            rect = win32gui.GetWindowRect(self.window_snap.hwnd)
            l, t, r, b = rect
            self.window_snap.width  = r - l
            self.window_snap.height = b - t
        except Exception:
            pass
        self.window_snap.base_span = 0.0
        self.span_smoother.reset()
        self.state = AppState.SCALE
        print(f"SCALE  -> '{self.window_snap.title}'")

    def _stop(self):
        self.state = AppState.IDLE
        self.left_smoother.reset()
        self.right_smoother.reset()
        self.span_smoother.reset()
        self.window_snap.both_fist_frames  = 0
        self.window_snap.scale_hold_frames = 0
        print("IDLE   -- released")

    def _close_grabbed(self):
        self.win_manager.close_window(self.window_snap.hwnd)
        print(f"CLOSE  -> '{self.window_snap.title}'")
        self._stop()
        self._action_cooldown = 30

    def _move_to_next_monitor(self):
        hwnd = self.window_snap.hwnd
        try:
            rect = win32gui.GetWindowRect(hwnd)
            cx = (rect[0] + rect[2]) // 2
            cy = (rect[1] + rect[3]) // 2
        except Exception:
            cx, cy = 0, 0
        curr = self.monitor_mgr.monitor_for(cx, cy)
        nxt  = self.monitor_mgr.next_monitor(curr)
        self.monitor_mgr.move_window_to_monitor(hwnd, nxt)
        print(f"MONITOR -> '{self.window_snap.title}'  to monitor {nxt}")
        self._action_cooldown = 20

    def _switch_monitor(self, idx: int):
        if idx == self.active_monitor_idx:
            return
        self.active_monitor_idx  = idx
        self.mapper.active_idx   = idx
        self._both_one_frames    = 0
        # Smoothers hold coords from the OLD monitor — reset so there's no jump
        self.left_smoother.reset()
        self.right_smoother.reset()
        # Reset one_frames so the gesture doesn't re-fire immediately
        for hand in (self.left_hand, self.right_hand):
            hand.one_frames = 0
            hand.is_one     = False
        mon = self.monitor_mgr.monitors[idx]
        print(f"ACTIVE MONITOR -> {idx}  ({mon[2]}x{mon[3]} @ {mon[0]},{mon[1]})")
        self._action_cooldown = 30

    # ── OVERLAY PUSH ───────────────────────────────────────────

    def _push_overlay(self):
        # Cap overlay update rate at 20fps.  Double-buffering eliminates GDI tearing,
        # so we can safely raise from the previous 15fps cap without visual artifacts.
        now = time.time()
        if now - self._last_overlay_push < 0.05:
            return
        self._last_overlay_push = now

        left_pos  = (int(self.left_hand.smooth_x),  int(self.left_hand.smooth_y)) \
                    if self.left_hand.landmarks is not None else None
        right_pos = (int(self.right_hand.smooth_x), int(self.right_hand.smooth_y)) \
                    if self.right_hand.landmarks is not None else None

        title = ''
        if self.state in (AppState.GRAB, AppState.SCALE):
            title = self.window_snap.title
        elif self.state == AppState.IDLE:
            for hand in (self.left_hand, self.right_hand):
                if hand.landmarks is not None and hand.fist_frames >= 2:
                    res = self.win_manager.find_window_at(
                        int(hand.smooth_x), int(hand.smooth_y)
                    )
                    if res:
                        title = res[1]
                    break

        close_pct = 0.0
        if self.state == AppState.GRAB and self.window_snap.both_fist_frames > 0:
            close_pct = self.window_snap.both_fist_frames / CLOSE_HOLD_FRAMES

        scale_pct = 0.0
        scale_hand_pos = None
        if self.state == AppState.GRAB and self.window_snap.scale_hold_frames > 0:
            scale_pct = self.window_snap.scale_hold_frames / SCALE_HOLD_FRAMES
            other = self.right_hand if self.window_snap.grab_side == 'L' else self.left_hand
            if other.landmarks is not None:
                scale_hand_pos = (int(other.smooth_x), int(other.smooth_y))

        # Monitor-switch progress driven by the shared simultaneous counter
        switch_pct = 0.0
        if self.state == AppState.IDLE and self._both_one_frames > 0:
            switch_pct = min(1.0, self._both_one_frames / MONITOR_TARGET_FRAMES)

        self.overlay.push(
            left_pos, right_pos, self.state, title, close_pct,
            active_monitor=self.monitor_mgr.monitors[self.active_monitor_idx],
            active_idx=self.active_monitor_idx,
            n_monitors=len(self.monitor_mgr.monitors),
            switch_pct=switch_pct,
            scale_pct=scale_pct,
            scale_hand_pos=scale_hand_pos,
        )

    # ── HUD OVERLAY (inside camera preview) ────────────────────

    def _draw_hud(self, frame: np.ndarray, w: int, h: int):
        bar = frame.copy()
        cv2.rectangle(bar, (0, 0), (w, 50), (20, 20, 20), -1)
        cv2.addWeighted(bar, 0.6, frame, 0.4, 0, frame)

        col_map = {AppState.IDLE: self.COL_IDLE,
                   AppState.GRAB: self.COL_GRAB,
                   AppState.SCALE: self.COL_SCALE}
        lbl_map = {
            AppState.IDLE:  "IDLE",
            AppState.GRAB:  f"GRAB   {self.window_snap.title[:35]}",
            AppState.SCALE: f"SCALE  {self.window_snap.title[:35]}",
        }
        col   = col_map[self.state]
        label = lbl_map[self.state]
        cv2.rectangle(frame, (10, 10), (22 + len(label) * 13, 40), col, -1)
        cv2.putText(frame, label, (15, 33),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.65, self.COL_BLACK, 2)

        cv2.putText(frame, f"{self.fps:.0f} fps", (w - 72, 20),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.48, self.COL_WHITE, 1)

        n = len(self.monitor_mgr.monitors)
        if n > 1:
            mon_label = f"MON {self.active_monitor_idx + 1}/{n}  [1/2/3]"
            cv2.putText(frame, mon_label, (w - 190, 44),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.42, (120, 200, 255), 1)

        snap = self.window_snap
        if self.state == AppState.GRAB and snap.both_fist_frames > 0:
            pct = snap.both_fist_frames / CLOSE_HOLD_FRAMES
            bw  = int(pct * 160)
            cv2.rectangle(frame, (10, 55), (170, 72), (40, 40, 40), -1)
            cv2.rectangle(frame, (10, 55), (10 + bw, 72), self.COL_RED, -1)
            cv2.putText(frame, "CLOSE", (15, 69),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.42, self.COL_WHITE, 1)

        if self.state == AppState.GRAB and snap.scale_hold_frames > 0:
            pct = snap.scale_hold_frames / SCALE_HOLD_FRAMES
            bw  = int(pct * 160)
            cv2.rectangle(frame, (10, 76), (170, 93), (40, 40, 40), -1)
            cv2.rectangle(frame, (10, 76), (10 + bw, 93), self.COL_SCALE, -1)
            cv2.putText(frame, "SCALE", (15, 90),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.42, self.COL_WHITE, 1)

        for hand, color in [
            (self.left_hand,  (100, 255, 100)),
            (self.right_hand, (100, 150, 255)),
        ]:
            if hand.landmarks is None:
                continue
            palm = (hand.landmarks[0] + hand.landmarks[9]) / 2.0
            fx, fy = int(palm[0] * w), int(palm[1] * h)
            if hand.is_fist:
                cv2.circle(frame, (fx, fy), 22, color, 3)
            elif hand.is_one or hand.is_two or hand.is_three:
                n_fingers = 1 if hand.is_one else (2 if hand.is_two else 3)
                r_outer = 18
                cv2.circle(frame, (fx, fy), r_outer, color, 1)
                # Progress arc driven by the shared simultaneous counter
                if hand.is_one and self._both_one_frames > 0:
                    pct = min(1.0, self._both_one_frames / MONITOR_TARGET_FRAMES)
                    axes = (r_outer + 4, r_outer + 4)
                    cv2.ellipse(frame, (fx, fy), axes, -90, 0, int(pct * 360), color, 2)
                cv2.putText(frame, str(n_fingers), (fx - 5, fy + 5),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 2)
            else:
                cv2.circle(frame, (fx, fy), 14, color, 1)

        if (self.state == AppState.SCALE
                and self.left_hand.landmarks is not None
                and self.right_hand.landmarks is not None):
            lp = (self.left_hand.landmarks[0]  + self.left_hand.landmarks[9])  / 2
            rp = (self.right_hand.landmarks[0] + self.right_hand.landmarks[9]) / 2
            lx, ly = int(lp[0] * w), int(lp[1] * h)
            rx, ry = int(rp[0] * w), int(rp[1] * h)
            cv2.line(frame, (lx, ly), (rx, ry), self.COL_SCALE, 2)

        cv2.rectangle(frame, (0, h - 30), (w, h), (20, 20, 20), -1)
        hints = '  |  '.join([
            'Fist=Grab', 'Both-☝️(1.5s)=NextMon',
            '2xFist(hold)=Close', '✌️+Grab=MoveToNextMon', '1/2/3key=Mon', 'Q=Quit',
        ])
        cv2.putText(frame, hints, (8, h - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.38, (160, 160, 160), 1)

    # ── CLEANUP ────────────────────────────────────────────

    def _cleanup(self):
        self.running = False
        self.overlay.stop()
        if self.inference:
            self.inference.stop()   # also calls hands.close() inside InferenceStream
        if self.cam:
            self.cam.stop()
        cv2.destroyAllWindows()
        try:
            ctypes.windll.winmm.timeEndPeriod(1)
        except Exception:
            pass
        print('\nGesture controller stopped.')


# -------------------------------------------------------------
#  CAMERA SELECTION
# -------------------------------------------------------------

def _get_camera_names_win() -> List[str]:
    """Query Windows PnP for friendly camera names in device-enum order."""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-PnpDevice | "
             "Where-Object { $_.Class -in @('Camera','Image') -and $_.Status -eq 'OK' } | "
             "Select-Object -ExpandProperty FriendlyName"],
            capture_output=True, text=True, timeout=8,
            creationflags=0x08000000,  # CREATE_NO_WINDOW
        )
        if result.returncode == 0 and result.stdout.strip():
            return [l.strip() for l in result.stdout.strip().splitlines() if l.strip()]
    except Exception:
        pass
    return []


def _enumerate_cameras(max_index: int = 6) -> List[Tuple[int, str]]:
    """Probe indices 0..max_index-1 and return list of (index, label)."""
    names = _get_camera_names_win()
    found = []
    for i in range(max_index):
        cap = cv2.VideoCapture(i, cv2.CAP_DSHOW)
        if not cap.isOpened():
            cap.release()
            cap = cv2.VideoCapture(i, cv2.CAP_MSMF)
        if not cap.isOpened():
            cap.release()
            continue
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        cap.release()
        name = names[len(found)] if len(found) < len(names) else f"Camera {i}"
        found.append((i, f"{name}  ({w}x{h})"))
    return found


def _select_camera() -> int:
    """Show camera selection menu and return chosen index."""
    print("\nDetecting cameras", end="", flush=True)
    cameras = _enumerate_cameras()
    print()

    if not cameras:
        print("  No cameras detected. Falling back to index 0.")
        return 0

    if len(cameras) == 1:
        idx, label = cameras[0]
        print(f"  One camera found: {label}")
        return idx

    print(f"\n  {len(cameras)} camera(s) found:\n")
    default_n = 1
    for n, (idx, label) in enumerate(cameras, 1):
        marker = "  <- default" if idx == WEBCAM_INDEX else ""
        print(f"    {n}. {label}{marker}")
        if idx == WEBCAM_INDEX:
            default_n = n
    print()

    while True:
        try:
            raw = input(f"  Select camera [1-{len(cameras)}] (Enter = [{default_n}]): ").strip()
            if raw == "":
                return cameras[default_n - 1][0]
            n = int(raw)
            if 1 <= n <= len(cameras):
                return cameras[n - 1][0]
        except KeyboardInterrupt:
            print("\n  Cancelled.")
            raise
        except ValueError:
            pass
        print(f"  Enter a number between 1 and {len(cameras)}.")


# -------------------------------------------------------------
#  ENTRY POINT
# -------------------------------------------------------------

if __name__ == '__main__':
    camera_idx = _select_camera()
    GestureWindowController(camera_idx=camera_idx).run()
