"""Group model: whichever window you grab becomes the mover, every other
window in its group just goes along with it.

Two behaviours are combined:

1. Instant follow (fallback): whichever window moves, all others in its
   group are translated by the same delta immediately. This is what happens
   for windows that don't go through a standard interactive drag (e.g.
   Aero Snap, or a move that skips the MOVESIZESTART/END hooks).

2. Drag / attraction effect (the default, nicer-looking path): while a
   window is being interactively dragged (between EVENT_SYSTEM_MOVESIZESTART
   and EVENT_SYSTEM_MOVESIZEEND) and after it's released, the rest of its
   group continuously eases ("is attracted") toward its exact correct
   position, over a time constant of ``return_ms``. The target position is
   recomputed every scheduler tick from the mover's live position, so
   followers start closing the gap immediately as you drag, not only once
   you let go.
"""

import json
import math
import os
import threading
import time

from . import win_api

PALETTE = [
    "#6C8EF5",  # blue
    "#F2795C",  # coral
    "#4FC28C",  # green
    "#E7B84B",  # amber
    "#B084F2",  # violet
    "#3FBFC0",  # teal
    "#F27BAE",  # pink
    "#8DA3B0",  # slate
]

DEFAULT_SETTINGS = {"return_ms": 250}
SCHEDULER_TICK = 0.015  # ~66 Hz
ECHO_GRACE = 0.3  # ignore MOVESIZESTART on a non-mover only if we touched it this recently
STALE_DRAG_TIMEOUT = 1.5  # auto-finalize a drag if the mover stops reporting (missed MOVESIZEEND)


class _DragSession:
    """Tracks one active drag. Whichever window you grab becomes the mover;
    every other member of its group continuously eases toward its correct
    position (computed live from the mover's current position) while
    dragging, then glides to its exact final position over ``return_ms``
    once released."""

    __slots__ = (
        "mover", "base_rects", "last_seen", "dragging",
        "follower_cur", "released_at", "release_start",
    )

    def __init__(self, mover, base_rects):
        self.mover = mover
        self.base_rects = base_rects  # hwnd -> Rect, snapshot at drag start
        self.last_seen = time.monotonic()  # last time the mover reported a move
        self.dragging = True
        self.follower_cur = {}  # hwnd -> Rect, current eased position (float-precision)
        self.released_at = None  # monotonic time the mover was released, once known
        self.release_start = None  # hwnd -> Rect, follower position at the moment of release


class Group:
    def __init__(self, group_id, members, color=None, locked=True):
        """members: dict hwnd -> {"title": str, "class": str}"""
        self.id = group_id
        self.members = members  # hwnd -> meta
        self.color = color or PALETTE[(group_id - 1) % len(PALETTE)]
        self.locked = locked  # while unlocked, windows move independently (no pinning)

    def hwnds(self):
        return list(self.members.keys())

    def to_dict(self):
        return {
            "id": self.id,
            "color": self.color,
            "locked": self.locked,
            "members": [
                {"hwnd": hwnd, "title": meta["title"], "class": meta["class"]}
                for hwnd, meta in self.members.items()
            ],
        }


class GroupManager:
    def __init__(self, config_path):
        self.config_path = config_path
        self.groups = {}  # id -> Group
        self._last_rect = {}  # hwnd -> Rect (our authoritative cache)
        self._lock = threading.Lock()
        self._next_id = 1
        self._enabled = True
        self._settings = dict(DEFAULT_SETTINGS)
        self._drag = {}  # group_id -> _DragSession
        self._last_programmatic_move = {}  # hwnd -> monotonic time we last moved it ourselves
        self._last_foreground = 0
        self._scheduler_thread = None
        self._scheduler_stop = threading.Event()

    # ---------------------------------------------------------- persistence
    def load(self):
        if not os.path.exists(self.config_path):
            return []
        with open(self.config_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        unresolved = []
        with self._lock:
            settings = data.get("settings", {})
            if isinstance(settings, dict):
                for key in ("return_ms",):
                    if key in settings:
                        try:
                            self._settings[key] = max(0, min(int(settings[key]), 3000))
                        except (TypeError, ValueError):
                            pass

            for group_data in data.get("groups", []):
                members = {}
                for m in group_data["members"]:
                    hwnd = win_api.find_window_by_title_class(m["title"], m["class"])
                    if hwnd and win_api.is_window_valid(hwnd):
                        members[hwnd] = {"title": m["title"], "class": m["class"]}
                    else:
                        unresolved.append(m["title"])
                if len(members) >= 2:
                    gid = self._next_id
                    self._next_id += 1
                    self.groups[gid] = Group(gid, members, color=group_data.get("color"))
                    for hwnd in members:
                        rect = win_api.get_window_rect(hwnd)
                        if rect:
                            self._last_rect[hwnd] = rect
        return unresolved

    def save(self):
        with self._lock:
            data = {
                "settings": dict(self._settings),
                "groups": [
                    {
                        "color": group.color,
                        "members": [
                            {"title": meta["title"], "class": meta["class"]}
                            for meta in group.members.values()
                        ],
                    }
                    for group in self.groups.values()
                ],
            }
        tmp_path = self.config_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, self.config_path)

    # ------------------------------------------------------------- editing
    def create_group(self, hwnd_list):
        """hwnd_list: list of hwnds (>=2) to link together."""
        if len(hwnd_list) < 2:
            return None
        with self._lock:
            members = {}
            for hwnd in hwnd_list:
                title = None
                cls = None
                try:
                    import win32gui
                    title = win32gui.GetWindowText(hwnd)
                    cls = win32gui.GetClassName(hwnd)
                except Exception:
                    pass
                members[hwnd] = {"title": title or "", "class": cls or ""}
                rect = win_api.get_window_rect(hwnd)
                if rect:
                    self._last_rect[hwnd] = rect
            gid = self._next_id
            self._next_id += 1
            group = Group(gid, members)
            self.groups[gid] = group
        self.save()
        return group

    def remove_group(self, group_id):
        with self._lock:
            self.groups.pop(group_id, None)
            self._drag.pop(group_id, None)
        self.save()

    def list_groups(self):
        with self._lock:
            return list(self.groups.values())

    def set_enabled(self, enabled):
        self._enabled = bool(enabled)

    def is_enabled(self):
        return self._enabled

    def group_for_hwnd(self, hwnd):
        with self._lock:
            for group in self.groups.values():
                if hwnd in group.members:
                    return group
        return None

    def get_group(self, group_id):
        with self._lock:
            return self.groups.get(group_id)

    def set_group_locked(self, group_id, locked):
        """Unlocking pauses pinning for this group entirely — its windows can
        be freely rearranged. Re-locking captures wherever they currently are
        as the new baseline, so future moves propagate correctly from there."""
        with self._lock:
            group = self.groups.get(group_id)
            if group is None:
                return None
            group.locked = bool(locked)
            if not group.locked:
                self._drag.pop(group_id, None)
            else:
                for hwnd in group.members:
                    rect = win_api.get_window_rect(hwnd)
                    if rect:
                        self._last_rect[hwnd] = rect
            return group

    # -------------------------------------------------------------- settings
    def get_settings(self):
        with self._lock:
            return dict(self._settings)

    def set_settings(self, return_ms=None):
        with self._lock:
            if return_ms is not None:
                self._settings["return_ms"] = max(0, min(int(return_ms), 3000))
            result = dict(self._settings)
        self.save()
        return result

    # --------------------------------------------------------------- events
    def on_window_destroyed(self, hwnd):
        with self._lock:
            self._last_rect.pop(hwnd, None)
            changed = False
            for group in list(self.groups.values()):
                if hwnd in group.members:
                    del group.members[hwnd]
                    changed = True
                    self._drag.pop(group.id, None)
                    if len(group.members) < 2:
                        del self.groups[group.id]
        if changed:
            self.save()

    def on_foreground_changed(self, hwnd):
        """When a window becomes the active one, bring the rest of its group
        just below it in z-order so they don't get left behind other apps."""
        self._raise_group_siblings(hwnd)

    def _raise_group_siblings(self, hwnd):
        if not self._enabled:
            return
        with self._lock:
            owner_group = None
            for group in self.groups.values():
                if hwnd in group.members:
                    owner_group = group
                    break
            if owner_group is None or not owner_group.locked:
                return
            other_hwnds = [h for h in owner_group.members if h != hwnd]

        insert_after = hwnd
        for other_hwnd in other_hwnds:
            win_api.raise_below(other_hwnd, insert_after)
            insert_after = other_hwnd

    def on_move_start(self, hwnd):
        """User grabbed a window's title bar (or resize border). If it's
        part of a locked group, snapshot everyone's current position so we
        can compute where they need to end up as you drag / once you
        release."""
        if not self._enabled:
            return
        with self._lock:
            owner_group = None
            for group in self.groups.values():
                if hwnd in group.members:
                    owner_group = group
                    break
            if owner_group is None or not owner_group.locked:
                return

            resolved = {}
            existing = self._drag.get(owner_group.id)
            if existing is not None:
                if hwnd != existing.mover:
                    # Windows can fire MOVESIZESTART/END for a window we are
                    # currently repositioning ourselves via rapid
                    # SetWindowPos calls, even though nobody is actually
                    # dragging it. Only treat this as that kind of echo if we
                    # genuinely touched this exact window very recently —
                    # otherwise a stale/stuck session (e.g. a missed
                    # MOVESIZEEND) would permanently block every OTHER
                    # window in the group from ever becoming the mover again.
                    last_touch = self._last_programmatic_move.get(hwnd, 0.0)
                    if time.monotonic() - last_touch < ECHO_GRACE:
                        return
                # Whether it's the same leader being re-grabbed before its
                # group finished easing into place, or a different window
                # taking over a stuck session, compute the previous
                # session's followers' TRUE final positions to use as the
                # new baseline — but WITHOUT physically moving anything.
                # Snapping them there instantly (as an earlier version of
                # this fix did) looked like a sudden jerk/teleport on every
                # regrab; correcting only the bookkeeping and letting the
                # normal per-tick easing continue from wherever a follower
                # actually is right now keeps the motion smooth while still
                # fixing the underlying bug — the new baseline would
                # otherwise get snapshotted from wherever followers happen
                # to be mid-ease (not yet arrived), corrupting where the
                # whole group thinks it belongs from then on.
                resolved = self._true_targets(existing)
                self._drag.pop(owner_group.id, None)

            base_rects = {}
            for h in owner_group.members:
                if h != hwnd and h in resolved:
                    base_rects[h] = resolved[h]
                    continue
                rect = self._last_rect.get(h) or win_api.get_window_rect(h)
                if rect is None:
                    return  # can't establish a baseline for this group right now
                base_rects[h] = rect

            session = _DragSession(hwnd, base_rects)
            for h in owner_group.members:
                if h == hwnd:
                    continue
                # Seed the eased "current" position from wherever the
                # window actually, physically is right now (it may still be
                # mid-glide from the previous session) — not from the
                # corrected baseline — so the new session continues the
                # glide smoothly toward the (now correct) target instead of
                # jumping there.
                session.follower_cur[h] = self._last_rect.get(h) or base_rects[h]
            self._drag[owner_group.id] = session

    def _true_targets(self, session):
        """Assumes self._lock is held. Returns {hwnd: Rect} — each
        follower's TRUE final (resting) position for a session that's
        ending or being superseded, computed from the mover's current
        position. Pure bookkeeping: does not move anything or touch the
        rect cache."""
        hwnd = session.mover
        mover_rect = self._last_rect.get(hwnd) or session.base_rects[hwnd]
        base_mover = session.base_rects[hwnd]
        dx = mover_rect.left - base_mover.left
        dy = mover_rect.top - base_mover.top

        targets = {}
        for h, base in session.base_rects.items():
            if h == hwnd:
                continue
            targets[h] = base.translated(dx, dy)
        return targets

    def on_move_end(self, hwnd):
        """User released the window. Its group's followers (already most of
        the way there, since they've been tracking it live during the drag)
        glide from wherever they currently are to this exact final position
        over ``return_ms``."""
        if not self._enabled:
            return
        with self._lock:
            for session in self._drag.values():
                if session.mover != hwnd or not session.dragging:
                    continue
                final_rect = win_api.get_window_rect(hwnd) or self._last_rect.get(hwnd) or session.base_rects[hwnd]
                self._last_rect[hwnd] = final_rect
                session.dragging = False
                session.released_at = time.monotonic()
                session.release_start = dict(session.follower_cur)

    def on_location_changed(self, hwnd):
        if not self._enabled:
            return

        with self._lock:
            owner_group = None
            for group in self.groups.values():
                if hwnd in group.members:
                    owner_group = group
                    break
            if owner_group is None or not owner_group.locked:
                return

            new_rect = win_api.get_window_rect(hwnd)
            if new_rect is None:
                return
            old_rect = self._last_rect.get(hwnd)
            if old_rect is None:
                self._last_rect[hwnd] = new_rect
                return

            session = self._drag.get(owner_group.id)

            if session is not None:
                if session.dragging and session.mover == hwnd:
                    # The mover is being dragged; the scheduler tick reads
                    # this cached rect to keep followers' target live, so
                    # just update it and track liveness for the stale-drag
                    # watchdog below — no propagation from here.
                    session.last_seen = time.monotonic()
                    self._last_rect[hwnd] = new_rect
                    return
                # Any other movement of a group member while a session owns
                # this group must be OUR OWN scheduler doing its job (easing
                # a follower toward its target) — never re-propagate it, even
                # if it doesn't exactly match our cache (rounding/timing).
                # Doing so would feed this echo back into the whole group as
                # if it were a fresh user move, causing visible jitter.
                self._last_rect[hwnd] = new_rect
                return

            dx = new_rect.left - old_rect.left
            dy = new_rect.top - old_rect.top
            if dx == 0 and dy == 0:
                return  # size-only change, or just an echo of our own move

            # Classic instant fallback: no managed drag session for this
            # group at all (a non-standard move that skipped the
            # MOVESIZESTART/END hooks entirely, e.g. Aero Snap).
            self._last_rect[hwnd] = new_rect
            moves = []
            for other_hwnd in list(owner_group.members.keys()):
                if other_hwnd == hwnd:
                    continue
                other_rect = self._last_rect.get(other_hwnd)
                if other_rect is None:
                    other_rect = win_api.get_window_rect(other_hwnd)
                    if other_rect is None:
                        continue
                target = other_rect.translated(dx, dy)
                moves.append((other_hwnd, target))
                self._last_rect[other_hwnd] = target

        # Move every follower together in one frame instead of one
        # SetWindowPos call at a time (see move_windows_batch docstring).
        win_api.move_windows_batch(moves)

    # ------------------------------------------------------------ scheduler
    def start(self):
        """Start the background thread driving the settle animation.
        Independent of the WinEvent hook thread."""
        self._scheduler_stop.clear()
        self._scheduler_thread = threading.Thread(target=self._scheduler_loop, daemon=True)
        self._scheduler_thread.start()

    def stop(self):
        self._scheduler_stop.set()
        if self._scheduler_thread:
            self._scheduler_thread.join(timeout=2)

    def _scheduler_loop(self):
        while not self._scheduler_stop.is_set():
            try:
                self._tick()
            except Exception:
                pass
            self._scheduler_stop.wait(SCHEDULER_TICK)

    def _poll_foreground(self):
        """EVENT_SYSTEM_FOREGROUND doesn't fire reliably for every window /
        app (same class of gap as MOVESIZESTART/END), so back the WinEvent
        hook with a cheap poll here as a catch-all."""
        fg = win_api.get_foreground_window()
        if fg != self._last_foreground:
            self._last_foreground = fg
            self._raise_group_siblings(fg)

    def _tick(self):
        self._poll_foreground()
        now = time.monotonic()
        moves = []  # (hwnd, Rect) across every active session this tick
        with self._lock:
            return_ms = self._settings.get("return_ms", 0)
            duration = max(return_ms, 0) / 1000.0
            if return_ms <= 0:
                drag_alpha = 1.0  # no smoothing: followers snap straight to target every tick
            else:
                drag_alpha = 1 - math.exp(-SCHEDULER_TICK / duration)

            finished = []
            for gid, session in list(self._drag.items()):
                if session.dragging and now - session.last_seen > STALE_DRAG_TIMEOUT:
                    # MOVESIZEEND was likely dropped (missed WinEvent) —
                    # without this, the group would stay locked to this
                    # mover forever and no other window could take over.
                    session.dragging = False
                    session.released_at = now
                    session.release_start = dict(session.follower_cur)

                mover_rect = self._last_rect.get(session.mover) or session.base_rects[session.mover]
                base_mover = session.base_rects[session.mover]
                dx = mover_rect.left - base_mover.left
                dy = mover_rect.top - base_mover.top

                if session.dragging:
                    # Still being dragged: continuously ease every follower
                    # toward its live target (recomputed from the mover's
                    # current position), so they visibly start closing the
                    # gap right away instead of waiting for release.
                    for h, base in session.base_rects.items():
                        if h == session.mover:
                            continue
                        target = base.translated(dx, dy)
                        cur = session.follower_cur.get(h, target)
                        new = target if drag_alpha >= 1.0 else cur.lerp(target, drag_alpha)
                        session.follower_cur[h] = new
                        rounded = new.rounded()
                        moves.append((h, rounded))
                        self._last_rect[h] = rounded
                        self._last_programmatic_move[h] = now
                else:
                    # Released: deterministic ease-out from wherever each
                    # follower was at the moment of release to its exact
                    # final position, landing exactly at `duration`.
                    elapsed = now - session.released_at
                    frac = 1.0 if duration <= 0 else min(elapsed / duration, 1.0)
                    eased = 1 - (1 - frac) ** 3
                    for h, base in session.base_rects.items():
                        if h == session.mover:
                            continue
                        target = base.translated(dx, dy)
                        start = session.release_start.get(h, target)
                        new = target if frac >= 1.0 else start.lerp(target, eased)
                        session.follower_cur[h] = new
                        rounded = new.rounded()
                        moves.append((h, rounded))
                        self._last_rect[h] = rounded
                        self._last_programmatic_move[h] = now
                    if frac >= 1.0:
                        finished.append(gid)
            for gid in finished:
                self._drag.pop(gid, None)

        # All windows moving this tick — across every active group — land
        # together in one DeferWindowPos batch instead of a visible stagger.
        win_api.move_windows_batch(moves)
