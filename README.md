# Hand Gesture Window Controller

Control Windows windows through hand gestures captured by a webcam. No special hardware required.

**Version:** 2.3 | **Python:** 3.10+ | **Platform:** Windows

---

## What it does

Detects 21 keypoints per hand in real time using MediaPipe, classifies the gesture, and translates it into Win32 actions: move, scale, minimize, close windows, and switch the active monitor in multi-monitor setups.

```
Webcam → CameraStream (thread) → InferenceStream (thread) → FSM → Win32 API
                                       MediaPipe 320x180              ↓
                                                             Win32Overlay (thread)
```

---

## Installation and usage

There are two ways to start the tool: the automatic launcher (`.bat`) or the
manual route (create/activate the `.venv` and run the `.py` yourself).

### Option 1 — Automatic launcher (recommended)

Double-click (or run) **`install_and_run.bat`**. The script:

1. Verifies that Python 3.10+ is installed
2. Creates a local virtual environment in `./.venv` (reuses it if it already exists)
3. Installs the packages with pinned versions
4. Audits vulnerabilities with `pip-audit`
5. Launches the application

This is the easiest path — it handles setup and launch in one step.

### Option 2 — Manual

**First time** (create the `.venv` and install dependencies):

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python .\gesture_window_controller.py
```

**Afterwards** (the `.venv` already exists — just activate and run):

```powershell
.\.venv\Scripts\Activate.ps1
python .\gesture_window_controller.py
```

> On PowerShell, if activation is blocked by the execution policy, run once:
> `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`

### To quit

Press **Q** or **ESC** in the camera window.

---

## Gestures

### ✊ Grab — Move window

Close your hand into a fist over the window you want to move. Move your hand to drag it. Open your hand to release.

### ✊✋ Scale — Resize window

While in GRAB, open your second hand. Move both hands apart or together to resize the window. Open both hands to exit.

### ✌️ Minimize — Minimize window

In the idle state, show two fingers (✌️) over the window to minimize.

### ✊✌️ Send to monitor — Move to another monitor

While in GRAB, show ✌️ with the other hand. The grabbed window is centered on the next monitor.

### ✊✊ Close — Close window

While in GRAB, also close the other hand into a fist and hold both for ~0.6s. The overlay shows a red progress bar.

### ☝️☝️ Switch monitor — Switch active monitor

In the idle state, show the index finger with both hands simultaneously and hold for ~1.5s. The overlay shows a cyan progress bar. This changes which monitor the gesture cursor maps to.

> You can also use the **1**, **2**, **3** keys (with the camera window focused) to switch monitors directly.

---

## State machine

```
                    fist over window
         +--------------------------------->  GRAB
         |                                      |
  IDLE <-+<--- release fist / open palms        |
         |                                      | second hand opened
         |                                      v
         +<---- both palms open ------------- SCALE
```

| State | What it does |
|---|---|
| `IDLE` | Monitors hands, waits for a fist or ✌️ over a window |
| `GRAB` | Moves the window following the hand |
| `SCALE` | Scales the window based on the distance between hands |

---

## Architecture

### Thread model

The system uses 4 threads so that no stage blocks the others:

```
Main thread
├── FSM: consumes InferenceStream results and updates state
├── cv2.imshow + waitKey (camera display at ~30fps)
└── Overlay push (rate-limited to 20fps)

CameraStream (daemon)
├── blocking cap.read() in a loop (~33ms per frame)
├── frame.copy() to isolate DirectShow's internal buffer
└── seq++ as a monotonic counter of new frames

InferenceStream (daemon)
├── Watches cam.seq to avoid processing the same frame twice
├── flip + resize to 320×180 + BGR→RGB
├── hands.process(rgb) — MediaPipe inference (~50–120ms on CPU)
└── result + seq protected with threading.Lock

Win32Overlay (daemon)
├── CreateWindowEx: native layered, topmost, transparent window
├── PumpMessages() → processes WM_PAINT in its own loop
└── GDI rendering with double buffering
```

**Why 4 threads:**

- `cap.read()` is blocking. In its own thread, the main loop never waits on the camera.
- `hands.process()` takes 50–120ms on CPU. Without separating it, the whole pipeline drops to 8–12fps. With the dedicated thread, the display runs at ~30fps and inference runs at its own pace in parallel.
- The overlay HWND needs its own Win32 message pump so it doesn't interfere with the one `cv2.imshow` uses internally.

### Synchronization between threads

| Shared data | Mechanism |
|---|---|
| `CameraStream.frame` | `frame.copy()` in the camera thread; atomic assignment via GIL |
| `CameraStream.seq` | Integer; atomic increment via GIL |
| `InferenceStream.result` | `threading.Lock()` in `run()` and `get()` |
| `InferenceStream.seq` | Protected by the same Lock |
| `Win32Overlay._data` | `threading.Lock()` in `push()` and `_render()` |
| Frame deduplication | `cam.seq != _last_cam_seq` and `inf.seq != _last_inf_seq` |

### Resolutions

| Purpose | Resolution | Why |
|---|---|---|
| Camera capture | 1280×720 (fallback 640×480 YUY2) | Image quality for the preview |
| MediaPipe inference | 320×180 | 44% fewer pixels than 480×270 → more fps; landmarks remain normalized (0–1) |
| Display window | 960×540 | Readable HUD with letterboxing |

### Native overlay

A transparent Win32 window that covers all monitors simultaneously.

- **Transparency:** `WS_EX_LAYERED` + `SetLayeredWindowAttributes` with a chroma key. The color `RGB(1,2,3)` (near-black, never produced by GDI) is the transparent color.
- **Double buffering:** all rendering happens on a reusable off-screen bitmap; a single `BitBlt(SRCCOPY)` at the end of each `WM_PAINT` copies it to the visible DC. No tearing.
- **Non-intrusive:** `WS_EX_TRANSPARENT` (clicks pass through), `WS_EX_NOACTIVATE` (never steals focus), `WS_EX_TOOLWINDOW` (hidden from the taskbar and Alt+Tab).

Rendered elements: hand cursors (green = left, blue = right), a diagonal line between hands in SCALE mode, an active-monitor badge with dots, progress bars for close and monitor switch, and a tooltip with the grabbed window's title.

---

## Components

### External libraries

**MediaPipe (Google)**
Google's ML framework for real-time landmark detection. It uses two cascaded neural networks: a Palm Detector that locates the palm in the frame (runs only when tracking is lost), and a Hand Landmark Model that, within that bounding box, locates 21 points (X, Y, Z normalized between 0 and 1) representing each joint of the hand. The project uses the lite model (`model_complexity=0`) with a minimum detection confidence of 0.7.

**Win32 API**
Windows' low-level programming interface. It is the layer the operating system exposes so applications can create windows, move them, close them, read their position and size, and draw graphics on screen. It is not an external library — it is part of Windows itself.

**pywin32**
A Python library that wraps the Win32 API and makes it accessible from Python. Without it, interacting with the operating system at this level would require writing C or C++ code. The Win32 functions this project uses are:

| Function | Purpose |
|---|---|
| `WindowFromPoint` + `GetAncestor` | Find the root window under the fist |
| `GetWindowRect` / `GetWindowText` | Read position, size, and title |
| `MoveWindow` | Move and resize |
| `ShowWindow` | Minimize or restore |
| `SetForegroundWindow` | Bring to front on grab |
| `PostMessage(WM_CLOSE)` | Close window |
| `EnumDisplayMonitors` | Enumerate the system's monitors |
| `CreateWindowEx` | Create the native overlay window |
| `SetLayeredWindowAttributes` | Chroma-key transparency |
| `PumpMessages` | The overlay's Win32 message loop |
| `BitBlt` | Atomic copy of the buffer to the visible DC (double buffering) |

**OpenCV (`cv2`)**
A computer vision library. In this project it is not used for computer vision but for infrastructure tasks: opening the webcam stream via DirectShow (Windows' video capture system), horizontally flipping the frame (mirror effect), converting between BGR↔RGB color spaces, and showing the preview window with the landmarks drawn on top.

**NumPy**
A vector math library. Each hand's 21 landmarks are represented as a NumPy `(21, 3)` array, which allows computing Euclidean distances between points in a single operation instead of a loop. It is also used for the EMA computation and the distance between hands in scale mode.

---

### Project classes

**GestureWindowController**
The main orchestrator. It runs the central loop, consumes the camera and inference thread results, executes the FSM, and coordinates all the other classes. It is the only component with a global view of the application state.

**GestureDetector**
Classifies a hand's gesture from the 21 landmarks. It uses no additional ML: for each finger it compares the tip-to-wrist distance against the knuckle-to-wrist distance. If the tip is farther away, the finger is extended. From the combination of extended/closed fingers it derives the gesture (✊, 🖐️, ☝️, ✌️).

**HandState**
Stores all of a hand's state: landmarks, smoothed position, active gesture, and debounce counters. It manages two independent grace windows: one for when MediaPipe detects the hand but the gesture is unclear (classifier jitter), and another for when MediaPipe loses the hand entirely for 1–2 frames (fast movement, momentary occlusion), preventing a brief drop from interrupting an in-progress grab.

**WindowSnapshot**
Captures a window's exact state at grab time: position, size, and hand position. It allows computing the movement delta frame by frame without accumulating drift.

**ScreenMapper**
Converts MediaPipe's normalized coordinates (0.0–1.0) to active-monitor pixels. It crops the outer 12% of the camera frame (`ACTIVE_MARGIN`), so the user doesn't need to reach the extreme edges to reach the monitor's edges.

**Smoother**
An EMA (Exponential Moving Average) filter that smooths the hand position by removing jitter. The formula is `smoothed = alpha × new + (1 - alpha) × previous_smoothed`. With `alpha = 0.35`, each new frame contributes 35% and the accumulated history 65%. Without this filter the window jitters because MediaPipe never returns exactly the same point two frames in a row.

**MonitorManager**
Enumerates the system's monitors and computes the virtual rectangle that spans all of them. It exposes methods to know which monitor contains a point, which is the next monitor (circular), and how to center a window on a target monitor.

**WindowManager**
A wrapper over `pywin32` for window manipulation: finding which window is under the cursor, moving and resizing it (with clamping to minimum and maximum sizes), minimizing it, and closing it.

---

## Configuration

Constants at the top of `gesture_window_controller.py`:

| Constant | Value | Description |
|---|---|---|
| `WEBCAM_INDEX` | `0` | Camera index |
| `SMOOTHING_ALPHA` | `0.35` | EMA smoothing (higher = more responsive) |
| `GESTURE_HOLD_FRAMES` | `4` | Consecutive frames to activate a gesture (~130ms) |
| `CLOSE_HOLD_FRAMES` | `18` | Frames to confirm a close (~0.6s) |
| `MONITOR_TARGET_FRAMES` | `45` | Frames of ☝️☝️ to switch monitor (~1.5s) |
| `ACTIVE_MARGIN` | `0.12` | Camera frame margin that is not mapped to the screen |
| `SCALE_SENSITIVITY` | `1.4` | Scale gesture amplifier |
| `DEBUG_OVERLAY` | `True` | Show/hide the HUD in the camera window |
| `EXCLUDED_TITLES` | `[...]` | Window titles that can never be grabbed |

---

## Dependencies

| Package | Version | Role |
|---|---|---|
| `mediapipe` | 0.10.21 | Hand detection (Google) |
| `opencv-python` | 4.11.0.86 | Webcam capture and display |
| `numpy` | 1.26.4 | Geometry and EMA smoothing |
| `pywin32` | 308 | Win32 API for window manipulation |

> `numpy` pinned below 2.0 due to `mediapipe==0.10.21` incompatibility with NumPy 2.x.

---

## Security

- **No network:** makes no network calls — everything is local
- **No storage:** does not save images or video to disk
- **No privileges:** does not require administrator permissions
- **Minimal access:** only reads window position and size, not their content
- Versions pinned to avoid automatic updates; use `pip-audit` before upgrading
