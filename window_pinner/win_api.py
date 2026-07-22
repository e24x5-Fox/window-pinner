"""Low-level Win32 helpers: window enumeration, rect get/set, and a
WinEvent hook that reports EVENT_OBJECT_LOCATIONCHANGE for any window."""

import ctypes
from ctypes import wintypes
import threading

import win32gui
import win32process
import win32con

user32 = ctypes.windll.user32

def _enable_dpi_awareness():
    """Make this process per-monitor DPI aware so GetWindowRect/SetWindowPos
    coordinates aren't virtualized/scaled relative to other (DPI-aware) apps
    on multi-monitor setups with different scaling factors."""
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


_enable_dpi_awareness()

EVENT_OBJECT_LOCATIONCHANGE = 0x800B
EVENT_OBJECT_DESTROY = 0x8001
EVENT_SYSTEM_FOREGROUND = 0x0003
EVENT_SYSTEM_MOVESIZESTART = 0x000A
EVENT_SYSTEM_MOVESIZEEND = 0x000B
WINEVENT_OUTOFCONTEXT = 0x0000
WINEVENT_SKIPOWNPROCESS = 0x0002
OBJID_WINDOW = 0
CHILDID_SELF = 0

WinEventProcType = ctypes.WINFUNCTYPE(
    None,
    wintypes.HANDLE,  # hWinEventHook
    wintypes.DWORD,   # event
    wintypes.HWND,    # hwnd
    wintypes.LONG,    # idObject
    wintypes.LONG,    # idChild
    wintypes.DWORD,   # idEventThread
    wintypes.DWORD,   # dwmsEventTime
)


class Rect:
    __slots__ = ("left", "top", "right", "bottom")

    def __init__(self, left, top, right, bottom):
        self.left = left
        self.top = top
        self.right = right
        self.bottom = bottom

    @property
    def width(self):
        return self.right - self.left

    @property
    def height(self):
        return self.bottom - self.top

    def __eq__(self, other):
        return (
            isinstance(other, Rect)
            and self.left == other.left
            and self.top == other.top
            and self.right == other.right
            and self.bottom == other.bottom
        )

    def __repr__(self):
        return f"Rect({self.left},{self.top},{self.right},{self.bottom})"

    def translated(self, dx, dy):
        return Rect(self.left + dx, self.top + dy, self.right + dx, self.bottom + dy)

    def lerp(self, other, t):
        return Rect(
            self.left + (other.left - self.left) * t,
            self.top + (other.top - self.top) * t,
            self.right + (other.right - self.right) * t,
            self.bottom + (other.bottom - self.bottom) * t,
        )

    def rounded(self):
        """Snap to integer pixels. Used before caching a computed (possibly
        interpolated) rect, so our cache always matches what SetWindowPos
        will actually apply — any drift here would look like a genuine user
        move and re-trigger propagation, causing jitter."""
        return Rect(round(self.left), round(self.top), round(self.right), round(self.bottom))


def get_window_rect(hwnd):
    try:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        return Rect(left, top, right, bottom)
    except win32gui.error:
        return None


def is_window_valid(hwnd):
    return bool(win32gui.IsWindow(hwnd))


def get_foreground_window():
    return win32gui.GetForegroundWindow()


def move_window(hwnd, rect: Rect):
    """Reposition a window without changing its size, z-order or focus."""
    try:
        win32gui.SetWindowPos(
            hwnd,
            0,
            int(round(rect.left)),
            int(round(rect.top)),
            0,
            0,
            win32con.SWP_NOSIZE
            | win32con.SWP_NOZORDER
            | win32con.SWP_NOACTIVATE
            | win32con.SWP_NOOWNERZORDER,
        )
    except win32gui.error:
        pass


user32.BeginDeferWindowPos.restype = wintypes.HANDLE
user32.BeginDeferWindowPos.argtypes = [ctypes.c_int]
user32.DeferWindowPos.restype = wintypes.HANDLE
user32.DeferWindowPos.argtypes = [
    wintypes.HANDLE, wintypes.HWND, wintypes.HWND,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT,
]
user32.EndDeferWindowPos.restype = wintypes.BOOL
user32.EndDeferWindowPos.argtypes = [wintypes.HANDLE]

_BATCH_MOVE_FLAGS = (
    win32con.SWP_NOSIZE
    | win32con.SWP_NOZORDER
    | win32con.SWP_NOACTIVATE
    | win32con.SWP_NOOWNERZORDER
)


def move_windows_batch(moves):
    """Reposition several windows in one go via DeferWindowPos, so they land
    on screen in the same paint pass instead of visibly staggering when
    repositioned one SetWindowPos call at a time (each individual call is a
    synchronous round-trip to the target window's message loop, so a plain
    Python loop over move_window() can show a few ms of stagger between
    windows — enough to look uncoordinated during a fast drag).

    moves: iterable of (hwnd, Rect).
    """
    moves = list(moves)
    if not moves:
        return
    if len(moves) == 1:
        move_window(*moves[0])
        return

    hdwp = user32.BeginDeferWindowPos(len(moves))
    if not hdwp:
        for hwnd, rect in moves:
            move_window(hwnd, rect)
        return

    ok = True
    for hwnd, rect in moves:
        hdwp = user32.DeferWindowPos(
            hdwp, hwnd, None,
            int(round(rect.left)), int(round(rect.top)), 0, 0,
            _BATCH_MOVE_FLAGS,
        )
        if not hdwp:
            ok = False
            break

    if hdwp:
        user32.EndDeferWindowPos(hdwp)
    if not ok:
        # Whatever DeferWindowPos couldn't batch, apply individually so a
        # single misbehaving window doesn't stall the rest.
        for hwnd, rect in moves:
            move_window(hwnd, rect)


def raise_below(hwnd, insert_after_hwnd):
    """Move hwnd to just below insert_after_hwnd in z-order, without
    activating it or changing its position/size."""
    try:
        win32gui.SetWindowPos(
            hwnd,
            insert_after_hwnd,
            0,
            0,
            0,
            0,
            win32con.SWP_NOMOVE
            | win32con.SWP_NOSIZE
            | win32con.SWP_NOACTIVATE
            | win32con.SWP_NOOWNERZORDER,
        )
    except win32gui.error:
        pass


def _is_candidate_window(hwnd):
    if not win32gui.IsWindowVisible(hwnd):
        return False
    if win32gui.GetParent(hwnd) != 0:
        return False
    title = win32gui.GetWindowText(hwnd)
    if not title.strip():
        return False
    ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
    if ex_style & win32con.WS_EX_TOOLWINDOW:
        return False
    return True


def list_windows():
    """Return a list of (hwnd, title, class_name, pid) for candidate top-level windows."""
    result = []

    def _enum(hwnd, _):
        if _is_candidate_window(hwnd):
            title = win32gui.GetWindowText(hwnd)
            cls = win32gui.GetClassName(hwnd)
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            result.append((hwnd, title, cls, pid))
        return True

    win32gui.EnumWindows(_enum, None)
    return result


def find_window_by_title_class(title, class_name):
    """Best-effort re-resolution of a window saved in a previous session."""
    for hwnd, wtitle, wclass, _pid in list_windows():
        if wtitle == title and wclass == class_name:
            return hwnd
    for hwnd, wtitle, wclass, _pid in list_windows():
        if wtitle == title:
            return hwnd
    return None


class WinEventWatcher:
    """Runs WinEventHooks for EVENT_OBJECT_LOCATIONCHANGE, EVENT_OBJECT_DESTROY
    and EVENT_SYSTEM_FOREGROUND in a dedicated thread with its own message
    pump, and forwards matching events to callbacks on that same thread."""

    def __init__(self, on_location_changed, on_destroyed, on_foreground_changed=None,
                 on_move_start=None, on_move_end=None):
        self._on_location_changed = on_location_changed
        self._on_destroyed = on_destroyed
        self._on_foreground_changed = on_foreground_changed
        self._on_move_start = on_move_start
        self._on_move_end = on_move_end
        self._thread = None
        self._hooks = []
        self._callback_ref = None  # keep alive
        self._thread_id = None
        self._ready = threading.Event()

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5)

    def stop(self):
        if self._thread_id:
            win32gui.PostThreadMessage(self._thread_id, win32con.WM_QUIT, 0, 0)
        if self._thread:
            self._thread.join(timeout=5)

    def _win_event_proc(self, hWinEventHook, event, hwnd, idObject, idChild,
                         idEventThread, dwmsEventTime):
        if idObject != OBJID_WINDOW or idChild != CHILDID_SELF or not hwnd:
            return
        if event == EVENT_OBJECT_LOCATIONCHANGE:
            try:
                self._on_location_changed(hwnd)
            except Exception:
                pass
        elif event == EVENT_OBJECT_DESTROY:
            try:
                self._on_destroyed(hwnd)
            except Exception:
                pass
        elif event == EVENT_SYSTEM_FOREGROUND:
            if self._on_foreground_changed:
                try:
                    self._on_foreground_changed(hwnd)
                except Exception:
                    pass
        elif event == EVENT_SYSTEM_MOVESIZESTART:
            if self._on_move_start:
                try:
                    self._on_move_start(hwnd)
                except Exception:
                    pass
        elif event == EVENT_SYSTEM_MOVESIZEEND:
            if self._on_move_end:
                try:
                    self._on_move_end(hwnd)
                except Exception:
                    pass

    def _run(self):
        import win32api
        self._thread_id = win32api.GetCurrentThreadId()

        self._callback_ref = WinEventProcType(self._win_event_proc)
        for event_id in (
            EVENT_OBJECT_LOCATIONCHANGE,
            EVENT_OBJECT_DESTROY,
            EVENT_SYSTEM_FOREGROUND,
            EVENT_SYSTEM_MOVESIZESTART,
            EVENT_SYSTEM_MOVESIZEEND,
        ):
            hook = user32.SetWinEventHook(
                event_id,
                event_id,
                0,
                self._callback_ref,
                0,
                0,
                WINEVENT_OUTOFCONTEXT,
            )
            self._hooks.append(hook)
        self._ready.set()
        try:
            win32gui.PumpMessages()
        finally:
            for hook in self._hooks:
                if hook:
                    user32.UnhookWinEvent(hook)
