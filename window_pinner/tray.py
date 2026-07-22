"""System tray icon with Show/Exit menu, built on pystray."""

from PIL import Image, ImageDraw
import pystray


def _make_icon_image():
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([4, 4, 60, 60], radius=10, fill=(45, 110, 200, 255))
    draw.rectangle([16, 22, 30, 34], fill=(255, 255, 255, 255))
    draw.rectangle([34, 30, 48, 42], fill=(255, 255, 255, 255))
    draw.line([23, 28, 41, 36], fill=(255, 200, 40, 255), width=3)
    return img


class TrayIcon:
    def __init__(self, on_show, on_exit):
        self._on_show = on_show
        self._on_exit = on_exit
        self.icon = pystray.Icon(
            "window_pinner",
            _make_icon_image(),
            "Window Pinner",
            menu=pystray.Menu(
                pystray.MenuItem("Открыть", self._show, default=True),
                pystray.MenuItem("Выход", self._exit),
            ),
        )

    def _show(self, icon, item):
        self._on_show()

    def _exit(self, icon, item):
        self._on_exit()
        icon.stop()

    def run(self):
        self.icon.run()

    def stop(self):
        self.icon.stop()
