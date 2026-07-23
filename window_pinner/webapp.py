"""Flask app exposing the window list / group management as a REST API,
plus the static single-page frontend."""

import os

from flask import Flask, jsonify, render_template, request

from . import demo_windows, win_api

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def create_app(group_manager):
    app = Flask(
        __name__,
        static_folder=os.path.join(_BASE_DIR, "static"),
        static_url_path="/static",
        template_folder=os.path.join(_BASE_DIR, "templates"),
    )

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/windows")
    def api_windows():
        windows = []
        for hwnd, title, cls, pid in win_api.list_windows():
            group = group_manager.group_for_hwnd(hwnd)
            windows.append(
                {
                    "hwnd": hwnd,
                    "title": title,
                    "class": cls,
                    "pid": pid,
                    "group_id": group.id if group else None,
                    "group_color": group.color if group else None,
                    "group_locked": group.locked if group else None,
                }
            )
        return jsonify(windows)

    @app.get("/api/groups")
    def api_groups_list():
        return jsonify([g.to_dict() for g in group_manager.list_groups()])

    @app.post("/api/groups")
    def api_groups_create():
        payload = request.get_json(force=True, silent=True) or {}
        hwnds = payload.get("hwnds") or []
        try:
            hwnds = [int(h) for h in hwnds]
        except (TypeError, ValueError):
            return jsonify({"error": "hwnds must be a list of integers"}), 400
        if len(hwnds) < 2:
            return jsonify({"error": "need at least 2 windows"}), 400
        valid = [h for h in hwnds if win_api.is_window_valid(h)]
        if len(valid) < 2:
            return jsonify({"error": "windows are no longer open"}), 400
        group = group_manager.create_group(valid)
        return jsonify(group.to_dict()), 201

    @app.delete("/api/groups/<int:group_id>")
    def api_groups_delete(group_id):
        group_manager.remove_group(group_id)
        return "", 204

    @app.post("/api/groups/<int:group_id>/lock")
    def api_groups_lock(group_id):
        group = group_manager.set_group_locked(group_id, True)
        if group is None:
            return jsonify({"error": "group not found"}), 404
        return jsonify(group.to_dict())

    @app.post("/api/groups/<int:group_id>/unlock")
    def api_groups_unlock(group_id):
        group = group_manager.set_group_locked(group_id, False)
        if group is None:
            return jsonify({"error": "group not found"}), 404
        return jsonify(group.to_dict())

    @app.post("/api/demo-windows")
    def api_demo_windows_create():
        number = demo_windows.spawn_demo_window()
        return jsonify({"number": number}), 201

    @app.get("/api/engine")
    def api_engine_get():
        return jsonify({"enabled": group_manager.is_enabled()})

    @app.post("/api/engine")
    def api_engine_set():
        payload = request.get_json(force=True, silent=True) or {}
        group_manager.set_enabled(bool(payload.get("enabled", True)))
        return jsonify({"enabled": group_manager.is_enabled()})

    @app.get("/api/settings")
    def api_settings_get():
        return jsonify(group_manager.get_settings())

    @app.post("/api/settings")
    def api_settings_set():
        payload = request.get_json(force=True, silent=True) or {}
        try:
            return_ms = int(payload["return_ms"]) if "return_ms" in payload else None
        except (TypeError, ValueError):
            return jsonify({"error": "return_ms must be an integer"}), 400
        result = group_manager.set_settings(return_ms=return_ms)
        return jsonify(result)

    return app
