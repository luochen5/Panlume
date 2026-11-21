"""Windows DPI 与 Tk scaling 的唯一初始化入口。"""

from __future__ import annotations

import os
import tkinter as tk


_dpi_initialized = False


def configure_windows_dpi() -> bool:
    global _dpi_initialized
    if os.name != "nt" or _dpi_initialized:
        return False
    import ctypes

    try:
        context = ctypes.c_void_p(-4)
        succeeded = ctypes.windll.user32.SetProcessDpiAwarenessContext(context)
    except (AttributeError, OSError) as exc:
        raise RuntimeError("Windows Per Monitor V2 DPI 感知初始化失败。") from exc
    if not succeeded:
        raise RuntimeError("Windows 未接受 Per Monitor V2 DPI 感知。")
    _dpi_initialized = True
    return True


def initialize_tk_scaling(root: tk.Tk) -> float:
    if getattr(root, "_panlume_tk_scaling_initialized", False):
        return float(root.tk.call("tk", "scaling"))
    dpi = float(root.winfo_fpixels("1i"))
    scaling = max(0.5, dpi / 72.0)
    root.tk.call("tk", "scaling", scaling)
    root._panlume_tk_scaling_initialized = True
    return scaling
