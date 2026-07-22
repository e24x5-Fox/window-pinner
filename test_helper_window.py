import sys
import tkinter as tk

title = sys.argv[1] if len(sys.argv) > 1 else "TestWin"
x = int(sys.argv[2]) if len(sys.argv) > 2 else 100
y = int(sys.argv[3]) if len(sys.argv) > 3 else 100

root = tk.Tk()
root.title(title)
root.geometry(f"300x200+{x}+{y}")
tk.Label(root, text=title, font=("Segoe UI", 16)).pack(expand=True)
root.mainloop()
