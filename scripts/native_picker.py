"""Run in a dedicated process, keeping GUI APIs out of server worker threads."""
import json
import sys
import tkinter as tk
from tkinter import filedialog

root = tk.Tk()
root.withdraw()
root.attributes('-topmost', True)
try:
    kind = sys.argv[1] if len(sys.argv) > 1 else 'files'
    if kind == 'folder':
        answer = filedialog.askdirectory(parent=root, title='FORGE — Choose folder', mustexist=True)
        paths = [answer] if answer else []
    elif kind == 'executable':
        answer = filedialog.askopenfilename(parent=root, title='FORGE — Choose executable', filetypes=[('Windows executable','*.exe'),('All files','*')])
        paths = [answer] if answer else []
    else:
        paths = list(filedialog.askopenfilenames(parent=root, title='FORGE — Select original files (no staging copy)', filetypes=[('All files','*')]))
    print(json.dumps({'paths': paths}, ensure_ascii=False))
finally:
    root.destroy()
