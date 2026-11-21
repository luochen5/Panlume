"""盘灯桌面浅色／深色主题。"""

from __future__ import annotations

import tkinter as tk
from tkinter import font, ttk


PALETTES = {
    "light": {
        "bg": "#F3F5F7", "surface": "#FFFFFF", "sidebar": "#172A3A",
        "sidebar_text": "#F4F7F9", "sidebar_muted": "#B7C4CD",
        "text": "#182631", "muted": "#586875", "accent": "#E4B34E",
        "accent_text": "#172A3A", "border": "#D9E0E5", "select": "#E9EEF2",
        "input": "#FFFFFF", "success": "#247052", "warning": "#8B5E16",
    },
    "dark": {
        "bg": "#111A22", "surface": "#1B2833", "sidebar": "#0C151C",
        "sidebar_text": "#EDF2F5", "sidebar_muted": "#A6B5C0",
        "text": "#E7EDF1", "muted": "#ACBBC5", "accent": "#E4B34E",
        "accent_text": "#172A3A", "border": "#354650", "select": "#2A3B47",
        "input": "#15212A", "success": "#74C69D", "warning": "#E9C46A",
    },
}


class DesktopTheme:
    def __init__(self, root: tk.Tk, name: str) -> None:
        self.root = root
        self.name = name if name in PALETTES else "light"
        self.colors = PALETTES[self.name]
        families = set(font.families(root))
        if "Microsoft YaHei UI" in families:
            self.family = "Microsoft YaHei UI"
        elif "Noto Sans CJK SC" in families:
            self.family = "Noto Sans CJK SC"
        else:
            self.family = "TkDefaultFont"
        self.apply()

    def apply(self) -> None:
        c = self.colors
        self.root.configure(bg=c["bg"])
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        base = (self.family, 10)
        style.configure(".", font=base, background=c["bg"], foreground=c["text"])
        style.configure("TFrame", background=c["bg"])
        style.configure("Surface.TFrame", background=c["surface"])
        style.configure("Sidebar.TFrame", background=c["sidebar"])
        style.configure("TLabel", background=c["bg"], foreground=c["text"])
        style.configure("Surface.TLabel", background=c["surface"], foreground=c["text"])
        style.configure("Muted.TLabel", background=c["bg"], foreground=c["muted"])
        style.configure("SurfaceMuted.TLabel", background=c["surface"], foreground=c["muted"])
        style.configure("Sidebar.TLabel", background=c["sidebar"], foreground=c["sidebar_text"])
        style.configure(
            "SidebarMuted.TLabel", background=c["sidebar"], foreground=c["sidebar_muted"]
        )
        style.configure("Title.TLabel", font=(self.family, 22, "bold"))
        style.configure("Section.TLabel", font=(self.family, 13, "bold"))
        style.configure("Metric.TLabel", font=(self.family, 18, "bold"))
        style.configure("TButton", padding=(14, 9), borderwidth=0)
        style.configure(
            "Primary.TButton", background=c["accent"], foreground=c["accent_text"],
            font=(self.family, 10, "bold"),
        )
        style.map(
            "Primary.TButton",
            background=[("active", "#F0C76C"), ("disabled", c["border"])],
            foreground=[("disabled", c["muted"])],
        )
        style.configure("Secondary.TButton", background=c["select"], foreground=c["text"])
        style.map("Secondary.TButton", background=[("active", c["border"])])
        for element in ("TEntry", "TCombobox", "TSpinbox"):
            style.configure(
                element, fieldbackground=c["input"], background=c["input"],
                foreground=c["text"], insertcolor=c["text"], bordercolor=c["border"],
                padding=6,
            )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", c["input"])],
            foreground=[("readonly", c["text"])],
            selectbackground=[("readonly", c["input"])],
            selectforeground=[("readonly", c["text"])],
        )
        style.configure("TCheckbutton", background=c["surface"], foreground=c["text"])
        style.map("TCheckbutton", background=[("active", c["surface"])])
        style.configure(
            "Treeview", background=c["input"], fieldbackground=c["input"],
            foreground=c["text"], bordercolor=c["border"], rowheight=30,
        )
        style.configure(
            "Treeview.Heading", background=c["select"], foreground=c["text"],
            font=(self.family, 10, "bold"), padding=6,
        )
        style.map(
            "Treeview", background=[("selected", c["accent"])],
            foreground=[("selected", c["accent_text"])],
        )
        style.configure("TSeparator", background=c["border"])
        style.configure("Horizontal.TProgressbar", background=c["accent"])
        for orientation in ("Vertical", "Horizontal"):
            style.layout(f"{orientation}.TScrollbar", [
                (
                    f"{orientation}.Scrollbar.trough",
                    {
                        "sticky": "nswe",
                        "children": [
                            (f"{orientation}.Scrollbar.thumb", {"sticky": "nswe"})
                        ],
                    },
                )
            ])
        style.configure(
            "TScrollbar",
            width=12,
            gripcount=0,
            borderwidth=0,
            relief="flat",
            background=c["muted"],
            troughcolor=c["bg"],
            darkcolor=c["muted"],
            lightcolor=c["muted"],
            bordercolor=c["bg"],
            arrowsize=0,
        )
        style.map(
            "TScrollbar",
            background=[("active", c["accent"]), ("pressed", c["accent"]), ("disabled", c["border"])],
            darkcolor=[("active", c["accent"]), ("pressed", c["accent"])],
            lightcolor=[("active", c["accent"]), ("pressed", c["accent"])],
        )
