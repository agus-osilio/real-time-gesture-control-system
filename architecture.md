# Hand Gesture Window Controller — Architecture Documentation

**Version:** 2.3  
**Date:** May 15, 2026  
**Main file:** `gesture_window_controller.py`  
**Language:** Python 3.10+

---

## Table of contents

1. [Overview](#1-overview)
2. [Packages used](#2-packages-used)
3. [System architecture](#3-system-architecture)
4. [Thread model](#4-thread-model)
5. [Classes and responsibilities](#5-classes-and-responsibilities)
6. [Available gestures](#6-available-gestures)
7. [Configuration](#7-configuration)
8. [Execution flow](#8-execution-flow)
9. [Security considerations](#9-security-considerations)

---

## 1. Overview

Software for controlling Windows windows through hand gestures captured by a webcam. No special hardware required.

The system detects 21 keypoints per hand in real time, classifies the gesture, and translates it into Win32 actions: move, scale, minimize, close windows, and switch the active monitor in multi-monitor setups.

```
Webcam → CameraStream (thread) → InferenceStream (thread) → FSM → Win32 API
                                       MediaPipe 320x180              ↓
                                                             Win32Overlay (thread)
```

### Resolutions in play

| Purpose | Resolution | Why |
|---|---|---|
| Camera capture | 1280×720 (or 640×480 YUY2 fallback) | Image quality for the preview |
| MediaPipe inference | 320×180 | 44% fewer pixels than 480×270 → more fps; landmarks normalized (0–1) |
| Window display | 960×540 | Readable HUD; easily resizable with letterboxing |

---

## 2. Packages used

### `mediapipe` — 0.10.21
**Developed by:** Google  
**Role:** Hand detection and tracking.

Two cascaded neural networks:
- **Palm Detector:** locates the palm in the frame (slower, runs only when the tracker loses the hand).
- **Hand Landmark Model:** within the palm bounding box, locates 21 points in normalized X, Y, Z.

Configuration used: `model_complexity=0` (lite model), `max_num_hands=2`, `min_detection_confidence=0.7`, `min_tracking_confidence=0.5`.

---

### `opencv-python` — 4.11.0.86
**Role:** Webcam capture, frame processing, and display.

Specific responsibilities:
- Open the stream with `cv2.VideoCapture` + DirectShow backend (`CAP_DSHOW`) + `MJPG` format
- Flip the frame horizontally (mirror effect) — done in `InferenceStream` before inference
- Convert BGR → RGB for MediaPipe
- Draw landmarks and HUD over the display frame
- Show the preview window with `cv2.imshow` and letterboxing

---

### `numpy` — 1.26.4
**Role:** Vector math.

Main uses:
- Landmarks as a `(21, 3)` array for vectorized operations
- Euclidean distances for gesture classification
- EMA (Exponential Moving Average) for position smoothing
- Span (distance) between hands in scale mode

> ⚠️ Version pinned below `2.0` due to `mediapipe==0.10.21` incompatibility with NumPy 2.x.

---

### `pywin32` — 308
**Role:** Interface with the Win32 API for window manipulation and the native overlay.

| Win32 function | Use |
|---|---|
| `win32gui.WindowFromPoint` | Find which window is under the fist |
| `win32gui.GetAncestor` | Walk up to the root window level |
| `win32gui.GetWindowRect` | Current position and size |
| `win32gui.GetWindowText` | Window title |
| `win32gui.MoveWindow` | Move and/or resize |
| `win32gui.ShowWindow` | Restore or minimize the window |
| `win32gui.SetForegroundWindow` | Bring to front on grab |
| `win32gui.PostMessage(WM_CLOSE)` | Close window |
| `win32api.EnumDisplayMonitors` | Enumerate the system's monitors |
| `win32gui.CreateWindowEx` | Create the native overlay window |
| `win32gui.SetLayeredWindowAttributes` | Color-key transparency |
| `win32gui.PumpMessages` | Win32 message loop in a dedicated thread |
| `win32gui.CreateCompatibleDC` | Off-screen DC for double buffering |
| `win32gui.CreateCompatibleBitmap` | Bitmap for the off-screen buffer |
| `win32gui.BitBlt` | Atomic copy of the buffer to the visible DC |
| `win32gui.Polyline` | True diagonal line in SCALE mode |

---

## 3. System architecture

```
+---------------------------------------------------------------------+
|                      GestureWindowController                         |
|           (orchestrator — FSM, global state, display)               |
|                                                                       |
|  +--------------+  +--------------+  +--------------+              |
|  |GestureDetect |  | ScreenMapper |  | WindowManager|              |
|  |(classifies 5 |  |(norm->screen,|  | (Win32 API)  |              |
|  | gestures/hand|  | multi-monitor|  |              |              |
|  +--------------+  +--------------+  +--------------+              |
|                                                                       |
|  +--------------+  +--------------+  +--------------+              |
|  |  HandState   |  |   Smoother   |  |MonitorManager|              |
|  |(data/debounce|  | (EMA filter) |  |(enum displays|              |
|  |  per hand)   |  |              |  | virtual rect)|              |
|  +--------------+  +--------------+  +--------------+              |
+---------------------------------------------------------------------+
          |                    |                    |
          v                    v                    v
+------------------+  +------------------+  +----------------------+
|   CameraStream   |  | InferenceStream  |  |     Win32Overlay     |
|  (daemon thread) |  |  (daemon thread) |  |   (daemon thread)    |
|  cap.read() loop |  | hands.process()  |  | PumpMessages + GDI   |
|  frame.copy()    |  | 320x180 RGB      |  | double-buffered      |
|  seq counter     |  | result + seq     |  | chroma-key transp.   |
+------------------+  +------------------+  +----------------------+
```

### State machine (FSM)

```
                    fist over window
         +--------------------------------->  GRAB
         |                                      |
  IDLE <-+<--- loses fist / open palms          |
         |                                      | open 2nd hand
         |                                      v
         +<---- both open palms ------------ SCALE
```

| State | What it does | Exit condition |
|---|---|---|
| `IDLE` | Monitors hands, waits for a fist or V over a window | Fist → GRAB; ✌️ → minimize |
| `GRAB` | Moves the window following the hand | Loses fist / second hand open / close gesture |
| `SCALE` | Scales the window based on the distance between hands | Both palms flat, or a hand is lost |

---

## 4. Thread model

The system uses **4 threads** simultaneously:

```
Main thread
├── FSM / gesture state updates (consumes InferenceStream results)
├── cv2.imshow + waitKey
└── Overlay push (rate-limited to 20fps)

CameraStream thread (daemon)
├── blocking cap.read() in a loop
├── frame.copy() → isolates DirectShow's buffer
└── seq++ for each new frame

InferenceStream thread (daemon)
├── Reads cam.frame when cam.seq changes
├── flip + resize to 320x180 + BGR→RGB
├── hands.process(rgb) — MediaPipe inference
└── result + seq protected with a Lock

Win32Overlay thread (daemon)
├── CreateWindowEx (layered, topmost, transparent)
├── PumpMessages() → processes WM_PAINT / WM_UPDATE
└── GDI rendering with double buffering
```

### Why 4 threads

**Separate CameraStream:** `cap.read()` is blocking (~33ms at 30fps). In its own thread, the main loop never waits on the camera.

**Separate InferenceStream:** `hands.process()` takes 50–120ms on CPU. It used to live in the main thread and capped the entire pipeline to 8–12fps. Now the display runs at camera speed (~30fps) and inference runs in parallel at its own speed. The two share the frame via `cam.frame` (the GIL guarantees atomic assignment) and synchronize with `seq` counters.

**Separate Win32Overlay:** a native HWND created and pumped in its own thread does not interfere with the message pump of `cv2.imshow` (which also uses Win32 internally).

### Synchronization

| Shared data | Mechanism |
|---|---|
| `CameraStream.frame` | `frame.copy()` in the camera thread; atomic assignment (GIL) |
| `CameraStream.seq` | Integer counter; atomic increment (GIL) |
| `InferenceStream.result` | `threading.Lock()` in `run()` and `get()` |
| `InferenceStream.seq` | Protected by the same Lock |
| `Win32Overlay._data` | `threading.Lock()` in `push()` and `_render()` |
| Frame deduplication | `cam.seq != _last_cam_seq` and `inf.seq != _last_inf_seq` |

---

## 5. Classes and responsibilities

### `CameraStream(threading.Thread)`
Daemon thread that keeps the latest camera frame available without blocking the main thread.

- Tries MJPG + 1280×720; if the driver rejects it, falls back to YUY2 + 640×480
- Drains 6 warm-up frames at startup
- `frame.copy()` on each read: isolates DirectShow's internal buffer
- `seq`: monotonic counter for deduplication

---

### `InferenceStream(threading.Thread)`
Daemon thread that runs MediaPipe's `hands.process()` continuously, decoupling inference from the display.

- Watches `cam.seq` so it doesn't process the same frame twice
- Does flip + resize to **320×180** before inferring (44% fewer pixels than 480×270)
- Exposes a non-blocking `get() → (result, seq)` so the main thread can consume results
- Owns the `mp.solutions.hands.Hands` instance (not the main thread)
- `stop()` calls `hands.close()` to release the TFLite models

---

### `GestureDetector`
Gesture classifier with no additional ML — uses pure geometry over the 21 landmarks.

**Core logic:** for each finger, compares tip→wrist distance vs knuckle→wrist distance:
```
dist(tip, wrist) > dist(mcp, wrist) × 1.1  →  finger extended
```

Returns 5 booleans: `(is_fist, is_open, is_one, is_two, is_three)`

| Gesture | Condition |
|---|---|
| `is_fist` ✊ | ≤1 finger extended AND not the index |
| `is_open` 🖐️ | ≥4 fingers extended |
| `is_one` ☝️ | Only the index extended |
| `is_two` ✌️ | Index + middle, rest closed |
| `is_three` 🤟 | Index + middle + ring, rest closed |

> `is_three` is implemented and classified but has no handler in the FSM — available for future gestures.

---

### `HandState`
Dataclass with all the state of one hand:

| Field | Description |
|---|---|
| `landmarks` | NumPy `(21, 3)` array with MediaPipe's points |
| `smooth_x`, `smooth_y` | Smoothed palm position in screen pixels |
| `is_fist/open/one/two/three` | Classifier result (debounced) |
| `fist/open/one/two/three_frames` | Consecutive-frame counter per gesture |
| `miss_frames` | Frames with the hand visible but no recognized gesture (internal grace) |
| `detect_miss` | Consecutive frames where MediaPipe did not detect the hand at all |
| `initialized` | True once the hand has been seen for the first time |

**Two independent grace windows:**

`miss_frames` handles classifier jitter: if MediaPipe detects the hand but does not classify a clear gesture, the active gesture counter is not reset until 3 consecutive frames of ambiguity.

`detect_miss` handles total loss of detection: if MediaPipe does not detect the hand at all (tracking drop from fast movement, occlusion, etc.), `landmarks` is not cleared immediately. Only after `HAND_DETECT_GRACE = 3` frames without detection are `landmarks` cleared and the gesture counters decayed. This prevents brief 1–2 frame drops from interrupting an in-progress grab.

---

### `WindowSnapshot`
Dataclass that saves the window's state at the exact moment of the grab:

| Field | Description |
|---|---|
| `hwnd` | The window's Win32 handle |
| `origin_win_x/y` | Window position at the start of the grab |
| `origin_hand_x/y` | Hand position at the start of the grab |
| `width`, `height` | Size at the start |
| `base_span` | Distance between hands at the start of scale mode |
| `base_width/height` | Window size captured at the start of scale mode |
| `grab_side` | `'L'` or `'R'` — which hand grabbed |
| `both_fist_frames` | Counter for the close gesture (both fists) |

Allows computing the movement **delta** frame by frame without accumulating drift.

---

### `MonitorManager`
Enumerates the system's monitors using `win32api.EnumDisplayMonitors` and computes the virtual rectangle that spans all of them.

Key methods:
- `monitor_for(x, y)` — which monitor contains the point (x, y)
- `next_monitor(idx)` — index of the next monitor (circular)
- `move_window_to_monitor(hwnd, target_idx)` — centers the window on the target monitor

---

### `WindowManager`
Wrapper over `pywin32` for window manipulation.

- `find_window_at(x, y)` — `WindowFromPoint` + `GetAncestor` to get the root window under the cursor, excluding system titles
- `move_window` — with clamping to minimum/maximum sizes
- `minimize_window` — via `ShowWindow(SW_MINIMIZE)`
- `close_window` — via `PostMessage(WM_CLOSE)`

---

### `ScreenMapper`
Converts MediaPipe's normalized coordinates (0.0–1.0) to active-monitor pixels.

**ACTIVE_MARGIN = 0.12:** crops the outer 12% of the camera frame. The central 76% of the frame maps to 100% of the monitor. The user does not need to bring their hands to the extreme edges of the camera.

```python
span = 1.0 - 2 * ACTIVE_MARGIN   # = 0.76
ax   = clamp((norm_x - ACTIVE_MARGIN) / span, 0, 1)
sx   = monitor_x + int(ax * monitor_width)
```

`active_idx` determines which monitor to map to. It changes with the monitor switch gesture.

---

### `Smoother`
EMA (Exponential Moving Average) filter to remove jitter:

```python
smooth = alpha * raw + (1 - alpha) * smooth
```

- `alpha = 0.35` by default: fluidity/responsiveness balance
- Separate instances: left hand, right hand, scale span (`alpha=0.4`)
- `reset()` on release or monitor switch, to avoid position jumps

---

### `Win32Overlay(threading.Thread)`
Transparent native Win32 window drawn over all monitors.

**Transparency technique:** `WS_EX_LAYERED` + `SetLayeredWindowAttributes` with a color key. The color `_COL_CHROMA = RGB(1, 2, 3)` (near-black, never produced by GDI) is the transparent color.

**Double buffering:** all rendering happens on an off-screen bitmap (`_mem_dc` / `_mem_bmp`) created once when the window starts and reused. At the end of each `WM_PAINT`, a single `BitBlt(SRCCOPY)` copies the full frame to the visible DC. DWM never sees a partially drawn frame → no tearing.

**Erase suppression:** the `WM_ERASEBKGND` handler returns 1 (erase handled) and `InvalidateRect` is called with `bErase=False`. This prevents the fully transparent frame flash that occurred between Windows' erase and the custom WM_PAINT.

**Window flags:**
- `WS_EX_TOPMOST` — always on top of everything
- `WS_EX_TRANSPARENT` — clicks pass through
- `WS_EX_NOACTIVATE` — never steals focus
- `WS_EX_TOOLWINDOW` — hidden from the taskbar and Alt+Tab

**Rendered elements:**
- Hand cursors (circle + crosshair): green = left, blue = right
- True diagonal line between hands in SCALE mode (via `Polyline`, not the previous `FillRect` hack)
- Active-monitor badge: pill with dots (● = active, • = inactive) + "MON N"
- Monitor switch progress bar (while holding ☝️☝️)
- Close progress bar (while holding both fists)
- Tooltip with the grabbed window's title

**Rate limit:** `_push_overlay()` in the main thread sends updates at most every 50ms (20fps). With double buffering, tearing no longer limits the rate, so it was raised from the previous 15fps.

---

### `GestureWindowController`
Main orchestrator.

**`run()`** — main loop with dual deduplication (camera and inference):
1. `waitKey(1)` → processes keyboard input (Q/ESC/1/2/3)
2. Gets `(inf_result, inf_seq)` from `InferenceStream.get()` and `cam_seq` from `CameraStream.seq`
3. If nothing is new from either source → `continue` (without burning CPU)
4. If there is a new inference result → `_update_hand()` / `_hand_lost()` + `_update_state()` + `_push_overlay()`
5. If there is a new camera frame → resize to 960×540, draw landmarks + HUD, letterbox, `imshow`

**State transition methods:**
- `_start_grab()` — window snapshot, transition to GRAB
- `_start_scale()` — capture base span, transition to SCALE
- `_stop()` — reset smoothers, back to IDLE
- `_close_grabbed()` — close window, 30-frame cooldown
- `_minimize_under()` — minimize the window under the cursor, 20-frame cooldown
- `_move_to_next_monitor()` — move the grabbed window to the next monitor
- `_switch_monitor(idx)` — change the cursor's active monitor, reset smoothers and counters

**Hand state update methods:**
- `_update_hand(hand, lm, smoother)` — updates landmarks, smoothing and gesture debounce; resets `detect_miss = 0`
- `_hand_lost(hand)` — applies the `HAND_DETECT_GRACE`-frame grace window before clearing `landmarks`

---

## 6. Available gestures

### ✌️ Minimize — Minimize window
**How:** in the IDLE state, show two fingers (✌️) over the window to minimize.  
**Effect:** `ShowWindow(SW_MINIMIZE)`. 20-frame cooldown to avoid double-firing.

---

### ✊ Grab — Grab and move
**How:** close your hand into a fist over the window to move.  
**How to exit:** open your hand.

```
delta_x = current_hand_x - start_hand_x
new_x   = start_window_x + delta_x
```

---

### 🤲 Scale — Scale the window
**How:** in the GRAB state, open the other hand. Move both hands apart or together.  
**How to exit:** open both hands.

```
scale  = current_span / base_span
new_w  = original_width × scale  (with the SCALE_SENSITIVITY = 1.4 amplifier)
```

The window is anchored to the midpoint between both hands.

---

### ✌️ Send to next monitor — Move window to another monitor
**How:** in the GRAB state, show two fingers (✌️) with the **other** hand.  
The grabbed window is centered on the next monitor.

---

### ✊✊ Close — Close window
**How:** in the GRAB state, also close the other hand into a fist and hold both for ~0.6s.  
The overlay shows a red progress bar. On completion, it sends `WM_CLOSE` to the window.

---

### ☝️☝️ Switch active monitor — Switch the active monitor
**How:** in the IDLE state, show the index finger with both hands simultaneously and hold for ~1.5s.  
The overlay shows a cyan progress bar. On completion, the hand cursor maps to the next monitor.

**Active monitor:** determines which monitor the hand coordinates map to. It does not move windows — it only changes the gesture cursor's control space.

**Also:** keyboard `1` / `2` / `3` with the camera window focused switches directly to the corresponding monitor.

---

### Gesture debounce

Each gesture needs `GESTURE_HOLD_FRAMES = 4` consecutive frames to activate (~130ms at 30fps).

There are **two independent** grace windows:

**Inside the classifier (`miss_frames`):** if MediaPipe detects the hand but the gesture is unclear, the active counter does not decay until 3 consecutive frames of ambiguity.

**At the detection level (`detect_miss`):** if MediaPipe does not detect the hand at all, `landmarks` is kept for `HAND_DETECT_GRACE = 3` frames before being cleared. This protects active grabs against brief tracker drops (fast movement, momentary occlusion).

For the monitor switch, the shared `_both_one_frames` timer only advances when **both** hands show ☝️ simultaneously.

---

## 7. Configuration

| Constant | Value | Description |
|---|---|---|
| `WEBCAM_INDEX` | `0` | Camera index |
| `FRAME_WIDTH` | `1280` | Requested capture resolution (width) |
| `FRAME_HEIGHT` | `720` | Requested capture resolution (height) |
| `SMOOTHING_ALPHA` | `0.35` | EMA smoothing. Higher = more responsive |
| `GESTURE_HOLD_FRAMES` | `4` | Consecutive frames to activate a gesture |
| `CLOSE_HOLD_FRAMES` | `18` | Frames to confirm a close (~0.6s at 30fps) |
| `MONITOR_TARGET_FRAMES` | `45` | Frames of ☝️☝️ for monitor switch (~1.5s at 30fps) |
| `HAND_DETECT_GRACE` | `3` | Frames before clearing the landmarks of an undetected hand |
| `ACTIVE_MARGIN` | `0.12` | Outer frame margin that is not mapped to the screen |
| `SCALE_SENSITIVITY` | `1.4` | Scale gesture amplifier |
| `MOVE_DEADZONE_PX` | `3` | Movements smaller than this are ignored |
| `MIN/MAX_WINDOW_WIDTH` | `300/3840` | Width limits when scaling |
| `MIN/MAX_WINDOW_HEIGHT` | `200/2160` | Height limits when scaling |
| `DEBUG_OVERLAY` | `True` | Show/hide the HUD in the camera window |
| `EXCLUDED_TITLES` | `[...]` | Window titles that can never be grabbed |

---

## 8. Execution flow

```
main()
  └── GestureWindowController.run()
        |
        ├── overlay.start()          # Win32Overlay thread starts
        ├── cv2.namedWindow (960x540, letterbox)
        ├── CameraStream.start()     # camera thread starts, drains 6 warm-up frames
        ├── InferenceStream.start()  # inference thread starts, consumes cam frames
        |
        └── main loop
              |
              ├── waitKey(1)                           # keyboard Q/ESC/1/2/3
              ├── inference.get() → (result, inf_seq)  # non-blocking
              ├── cam.seq → cam_seq
              ├── both unchanged? → continue           # without burning CPU
              |
              ├── [if new inf_seq]
              |     ├── detected_left / detected_right = False
              |     ├── for each hand in result:
              |     |     ├── _update_hand()           # landmarks + smoother + debounce
              |     |     └── detected_X = True
              |     ├── if not detected_left:  _hand_lost(left_hand)
              |     ├── if not detected_right: _hand_lost(right_hand)
              |     ├── _update_state()                # FSM
              |     |     ├── IDLE
              |     |     |     ├── ✌️ over window    → _minimize_under()
              |     |     |     ├── ☝️☝️ x45 frames  → _switch_monitor()
              |     |     |     └── ✊ over window    → _start_grab()
              |     |     |
              |     |     ├── GRAB
              |     |     |     ├── loses fist        → _stop()
              |     |     |     ├── ✌️ other hand      → _move_to_next_monitor()
              |     |     |     ├── ✊✊ x18 frames    → _close_grabbed()
              |     |     |     ├── 🖐️ other hand      → _start_scale()
              |     |     |     └── hand delta         → WindowManager.move_window()
              |     |     |
              |     |     └── SCALE
              |     |           ├── loses hand / 🖐️🖐️ → _stop()
              |     |           └── span between hands → WindowManager.move_window()
              |     |
              |     └── _push_overlay()                # rate-limited 20fps
              |
              └── [if new cam_seq]
                    ├── cam.frame + cv2.flip
                    ├── cv2.resize → 960x540
                    ├── mp_drawing.draw_landmarks()    # landmarks from the last inf
                    ├── _draw_hud()                    # FPS, state, monitor badge
                    └── cv2.imshow() with letterboxing
```

---

## 9. Security considerations

### Packages

Versions are pinned to avoid automatic updates. The PyPI ecosystem is under sustained attack (Shai-Hulud/TeamPCP campaigns, May 2026).

The 4 packages (`mediapipe`, `opencv-python`, `numpy`, `pywin32`) have not been compromised in known campaigns to date, but it is recommended to:

- Not run `pip install --upgrade` without reviewing changelogs and advisories
- Verify with `pip-audit` before upgrading
- Always install inside the local venv, never globally

### Execution

- **No network:** the script makes no network calls — everything is local
- **No storage:** does not save images or video to disk
- **Minimal access:** only reads window positions/sizes; does not access content
- **No privileges:** `MoveWindow`, `SetForegroundWindow`, `ShowWindow` are standard APIs that do not require admin

### Excluded windows

`EXCLUDED_TITLES` prevents grabbing system windows (`Program Manager`, `Windows Input Experience`) or the overlay's own window (`GestureOverlay`).
