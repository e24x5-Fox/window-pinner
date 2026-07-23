"""Floating on-screen badge attached to whichever window currently has
focus, anywhere in the system:

- If that window is already in a (locked or unlocked) group, the badge lets
  you lock/unlock that group right there.
- If it isn't in any group yet, the badge offers "add to group": click it on
  a few windows in turn (switching focus between them) to mark them, then
  click "Link" on the badge of any one of those marked windows to actually
  create the group from the whole marked set.

Only one window can be focused system-wide at a time, so a single Toplevel
badge is reused and repositioned/reconfigured every tick instead of keeping
one per window.

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
from .groups import SCHEDULER_TICK

# Match the group manager's own scheduler tick (~66 Hz) so the badge tracks
# its window's live position exactly as fast as the window itself moves —
# a slower poll here would make the badge visibly detach/lag behind during
# a fast drag even though the window is moving smoothly.
POLL_MS = max(1, round(SCHEDULER_TICK * 1000))
BADGE_GAP = 4  # gap between the badge and the window's top edge
NEUTRAL_DOT = "#5b6673"
SELECTED_DOT = "#4FC28C"


class _Badge:
    """A single reusable badge window with three mutually-exclusive faces:
    grouped / ungrouped-selectable / hidden."""

    def __init__(self, master, on_toggle_lock, on_toggle_pending, on_link):
        self._on_toggle_lock = on_toggle_lock
        self._on_toggle_pending = on_toggle_pending
        self._on_link = on_link
        self._hwnd = None

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

        self.main_btn = tk.Button(
            self.frame,
            text="",
            command=self._on_main_click,
            bd=0,
            padx=7,
            pady=2,
            font=("Segoe UI", 8),
            relief="flat",
            cursor="hand2",
        )
        self.main_btn.pack(side="left", padx=(0, 7), pady=4)

        self.link_btn = tk.Button(
            self.frame,
            text="",
            command=self._on_link_click,
            bd=0,
            padx=7,
            pady=2,
            font=("Segoe UI", 8, "bold"),
            relief="flat",
            cursor="hand2",
            bg="#173a2b",
            fg="#5be49b",
            activebackground="#1f4c39",
        )
        # packed/unpacked on demand, only when linking is possible

        self.top.update_idletasks()
        self._apply_native_styles()
        self.top.withdraw()

    def _apply_native_styles(self):
        try:
            hwnd = self.top.winfo_id()
            ex = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
            ex |= win32con.WS_EX_TOOLWINDOW | win32con.WS_EX_NOACTIVATE
            ex &= ~win32con.WS_EX_APPWINDOW
            win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, ex)
        except Exception:
            pass

    def _on_main_click(self):
        if self._hwnd is None:
            return
        if self._mode == "grouped":
            self._on_toggle_lock(self._group_id)
        elif self._mode == "ungrouped":
            self._on_toggle_pending(self._hwnd)

    def _on_link_click(self):
        self._on_link()

    def hide(self):
        self._hwnd = None
        self.top.withdraw()

    def show_grouped(self, hwnd, group, rect):
        self._hwnd = hwnd
        self._mode = "grouped"
        self._group_id = group.id

        self.dot.itemconfig(self.dot_id, fill=group.color)
        self.frame.configure(highlightbackground=group.color, highlightcolor=group.color)
        if group.locked:
            self.main_btn.configure(text="Открепить", bg="#1a1f27", fg="#eef1f5", activebackground="#242b39")
        else:
            self.main_btn.configure(text="Закрепить", bg="#3a2c12", fg="#ffb454", activebackground="#4a3818")
        self.link_btn.pack_forget()
        self._reposition(rect)

    def show_ungrouped(self, hwnd, rect, selected, can_link, pending_count):
        self._hwnd = hwnd
        self._mode = "ungrouped"

        self.dot.itemconfig(self.dot_id, fill=SELECTED_DOT if selected else NEUTRAL_DOT)
        self.frame.configure(highlightbackground=NEUTRAL_DOT, highlightcolor=NEUTRAL_DOT)
        if selected:
            self.main_btn.configure(text="Убрать", bg="#1a1f27", fg="#eef1f5", activebackground="#242b39")
        else:
            self.main_btn.configure(text="Добавить в группу", bg="#1a1f27", fg="#8da3b0", activebackground="#242b39")

        if can_link:
            self.link_btn.configure(text=f"Связать ({pending_count})")
            self.link_btn.pack(side="left", padx=(0, 7), pady=4)
        else:
            self.link_btn.pack_forget()

        self._reposition(rect)

    def _reposition(self, rect):
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
        self._badge = None
        self._pending = set()  # hwnds marked "add to group", awaiting Link

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
        self._badge = _Badge(self._root, self._toggle_lock, self._toggle_pending, self._link_pending)
        self._tick()
        self._root.mainloop()

    def _toggle_lock(self, group_id):
        group = self.group_manager.get_group(group_id)
        if group is not None:
            self.group_manager.set_group_locked(group_id, not group.locked)

    def _toggle_pending(self, hwnd):
        if hwnd in self._pending:
            self._pending.discard(hwnd)
        else:
            self._pending.add(hwnd)

    def _link_pending(self):
        hwnds = [h for h in self._pending if win_api.is_window_valid(h)]
        if len(hwnds) >= 2:
            self.group_manager.create_group(hwnds)
        self._pending.clear()

    def _tick(self):
        if self._stop.is_set():
            if self._badge is not None:
                self._badge.destroy()
                self._badge = None
            self._root.quit()
            return
        try:
            self._refresh()
        except Exception:
            pass
        self._root.after(POLL_MS, self._tick)

    def _refresh(self):
        # Drop any pending selection that's no longer valid or that already
        # belongs to a group by other means (e.g. created via the web UI).
        for hwnd in list(self._pending):
            if not win_api.is_window_valid(hwnd) or self.group_manager.group_for_hwnd(hwnd) is not None:
                self._pending.discard(hwnd)

        hwnd = win_api.get_foreground_window()
        if not hwnd or not win_api.is_window_valid(hwnd):
            self._badge.hide()
            return
        if not win32gui.IsWindowVisible(hwnd) or win32gui.IsIconic(hwnd):
            self._badge.hide()
            return

        group = self.group_manager.group_for_hwnd(hwnd)
        if group is None and not win_api.is_candidate_window(hwnd):
            self._badge.hide()
            return

        rect = win_api.get_window_rect(hwnd)
        if rect is None:
            self._badge.hide()
            return

        if group is not None:
            self._badge.show_grouped(hwnd, group, rect)
        else:
            selected = hwnd in self._pending
            can_link = selected and len(self._pending) >= 2
            self._badge.show_ungrouped(hwnd, rect, selected, can_link, len(self._pending))
