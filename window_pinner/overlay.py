"""Floating on-screen badges attached to every grouped window, showing which
group it belongs to and letting you unlock/lock that group right there —
no need to switch to the control panel to rearrange windows in place.

Runs its own Tkinter root in a dedicated thread; all Tk calls happen on
that thread (via a self-rescheduling ``after`` poll), so it's safe to call
into GroupManager (which has its own internal lock) from the button click
handlers without any extra cross-thread marshalling.
"""

import threading
import tkinter as tk

import win32con
import win32gui

from . import win_api

POLL_MS = 200
BADGE_GAP = 4  # gap between the badge and the window's top edge


class _Badge:
    def __init__(self, master, on_toggle):
        self._on_toggle = on_toggle

        self.top = tk.Toplevel(master)
        self.top.overrideredirect(True)
        self.top.attributes("-topmost", True)
        try:
            self.top.attributes("-alpha", 0.95)
        except tk.TclError:
            pass
        self.top.configure(bg="#11151b")

        self.frame = tk.Frame(self.top, bg="#11151b", highlightthickness=1)
        self.frame.pack()

        self.dot = tk.Canvas(self.frame, width=10, height=10, bg="#11151b", highlightthickness=0)
        self.dot_id = self.dot.create_oval(1, 1, 9, 9, fill="#888888", outline="")
        self.dot.pack(side="left", padx=(7, 4), pady=6)

        self.btn = tk.Button(
            self.frame,
            text="",
            command=self._on_click,
            bd=0,
            padx=7,
            pady=2,
            font=("Segoe UI", 8),
            relief="flat",
            cursor="hand2",
        )
        self.btn.pack(side="left", padx=(0, 7), pady=4)

        self.top.update_idletasks()
        self._apply_native_styles()
        self.top.withdraw()
        self._group_id = None

    def _apply_native_styles(self):
        try:
            hwnd = self.top.winfo_id()
            ex = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
            ex |= win32con.WS_EX_TOOLWINDOW | win32con.WS_EX_NOACTIVATE
            ex &= ~win32con.WS_EX_APPWINDOW
            win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, ex)
        except Exception:
            pass

    def _on_click(self):
        if self._group_id is not None:
            self._on_toggle(self._group_id)

    def update(self, group, rect, visible):
        self._group_id = group.id
        if not visible or rect is None:
            self.top.withdraw()
            return

        self.dot.itemconfig(self.dot_id, fill=group.color)
        self.frame.configure(highlightbackground=group.color, highlightcolor=group.color)
        if group.locked:
            self.btn.configure(text="Открепить", bg="#1a1f27", fg="#eef1f5", activebackground="#242b39")
        else:
            self.btn.configure(text="Закрепить", bg="#3a2c12", fg="#ffb454", activebackground="#4a3818")
        self.frame.configure(bg="#11151b")

        self.top.update_idletasks()
        badge_h = self.top.winfo_height() or 30

        x = max(rect.left, 0)
        y = rect.top - badge_h - BADGE_GAP
        if y < 0:
            # No room above (window flush with the screen's top edge) — fall
            # back to a small inset instead of floating off-screen.
            y = rect.top + BADGE_GAP
        self.top.geometry(f"+{int(x)}+{int(y)}")
        self.top.deiconify()
        try:
            win32gui.SetWindowPos(
                self.top.winfo_id(),
                win32con.HWND_TOPMOST,
                0, 0, 0, 0,
                win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE,
            )
        except Exception:
            pass

    def destroy(self):
        try:
            self.top.destroy()
        except Exception:
            pass


class OverlayManager:
    def __init__(self, group_manager):
        self.group_manager = group_manager
        self._stop = threading.Event()
        self._thread = None
        self._root = None
        self._badges = {}  # hwnd -> _Badge

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _run(self):
        self._root = tk.Tk()
        self._root.withdraw()
        self._tick()
        self._root.mainloop()

    def _toggle(self, group_id):
        group = self.group_manager.get_group(group_id)
        if group is not None:
            self.group_manager.set_group_locked(group_id, not group.locked)

    def _tick(self):
        if self._stop.is_set():
            for badge in self._badges.values():
                badge.destroy()
            self._badges.clear()
            self._root.quit()
            return
        try:
            self._refresh()
        except Exception:
            pass
        self._root.after(POLL_MS, self._tick)

    def _refresh(self):
        wanted = {}  # hwnd -> group
        for group in self.group_manager.list_groups():
            for hwnd in group.members:
                wanted[hwnd] = group

        for hwnd in list(self._badges.keys()):
            if hwnd not in wanted or not win_api.is_window_valid(hwnd):
                self._badges.pop(hwnd).destroy()

        foreground_hwnd = win_api.get_foreground_window()

        for hwnd, group in wanted.items():
            if not win_api.is_window_valid(hwnd):
                # Group membership hasn't caught up with reality yet (e.g. the
                # destroy notification is still in flight) — don't resurrect
                # a badge for a window that's already gone.
                continue
            if hwnd not in self._badges:
                self._badges[hwnd] = _Badge(self._root, self._toggle)
            rect = win_api.get_window_rect(hwnd)
            # Only the currently active member of the group shows its badge —
            # keeps the screen clutter-free and out of the way while you're
            # not actually interacting with that group.
            visible = (
                hwnd == foreground_hwnd
                and rect is not None
                and win32gui.IsWindowVisible(hwnd)
                and not win32gui.IsIconic(hwnd)
            )
            self._badges[hwnd].update(group, rect, visible)
