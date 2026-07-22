from window_pinner.tray import _make_icon_image

img = _make_icon_image()
img.save("icon.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (256, 256)])
print("icon.ico written")
