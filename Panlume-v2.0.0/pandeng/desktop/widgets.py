"""桌面通用控件；滚动页只负责布局，不承载业务逻辑。"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk


class ScrollablePage(ttk.Frame):
    """宽度随窗口变化、高度不足时可垂直滚动的页面容器。"""

    def __init__(self, master: tk.Misc, *, background: str) -> None:
        super().__init__(master)
        self.canvas = tk.Canvas(
            self,
            bg=background,
            bd=0,
            highlightthickness=0,
            takefocus=False,
        )
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(fill="both", expand=True)
        self.body = ttk.Frame(self.canvas)
        self._window = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self._bound_widgets: set[tk.Misc] = set()
        self._after_ids: set[str] = set()
        self._destroyed = False
        self.bind("<Destroy>", self._on_destroy_event)
        self.body.bind("<Configure>", self._on_body_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)

    def _on_body_configure(self, _event: tk.Event | None = None) -> None:
        if not self._destroyed:
            self._sync_scrollregion()

    def _on_canvas_configure(self, event: tk.Event) -> None:
        width = max(1, int(event.width))
        self.canvas.itemconfigure(self._window, width=width)
        self._update_wraplengths(width)
        self._schedule_idle(self._sync_scrollregion)

    def _update_wraplengths(self, width: int) -> None:
        """限制长说明的换行宽度，避免小窗口出现不可达的横向内容。"""
        available = max(220, width - 56)
        for widget in self._descendants(self.body):
            if not isinstance(widget, (tk.Label, ttk.Label)):
                continue
            try:
                original = int(widget.cget("wraplength"))
            except (tk.TclError, ValueError):
                continue
            if original <= 0:
                continue
            if not hasattr(widget, "_panlume_original_wraplength"):
                widget._panlume_original_wraplength = original  # type: ignore[attr-defined]
            original = widget._panlume_original_wraplength  # type: ignore[attr-defined]
            widget.configure(wraplength=min(original, available))

    @staticmethod
    def _descendants(parent: tk.Misc):
        for child in parent.winfo_children():
            yield child
            yield from ScrollablePage._descendants(child)

    def _schedule_idle(self, callback) -> None:
        if self._destroyed:
            return
        def run() -> None:
            self._after_ids.discard(after_id)
            if not self._destroyed:
                callback()
        after_id = self.after_idle(run)
        self._after_ids.add(after_id)

    def _sync_scrollregion(self) -> None:
        if self._destroyed or not self.canvas.winfo_exists():
            return
        canvas_height = max(1, self.canvas.winfo_height())
        content_height = max(1, self.body.winfo_reqheight())
        content_width = max(self.canvas.winfo_width(), self.body.winfo_reqwidth())
        self.canvas.configure(scrollregion=(0, 0, content_width, content_height))
        if content_height > canvas_height + 1:
            self.scrollbar.place(
                relx=1.0, rely=0.0, relheight=1.0, width=12, anchor="ne"
            )
        else:
            self.scrollbar.place_forget()
            self.canvas.yview_moveto(0.0)

    def bind_mousewheel(self) -> None:
        """在普通控件上滚动页面；文本框和表格保留自己的滚动行为。"""
        skipped = (tk.Text, ttk.Treeview, ttk.Scrollbar)
        for widget in (self, self.canvas, self.body, *self._descendants(self.body)):
            if isinstance(widget, skipped) or widget in self._bound_widgets:
                continue
            widget.bind("<MouseWheel>", self._on_mousewheel)
            widget.bind("<Button-4>", self._on_mousewheel)
            widget.bind("<Button-5>", self._on_mousewheel)
            self._bound_widgets.add(widget)

    def _on_mousewheel(self, event: tk.Event):
        if self.body.winfo_reqheight() <= self.canvas.winfo_height() + 1:
            return "break"
        if getattr(event, "num", None) == 4:
            units = -3
        elif getattr(event, "num", None) == 5:
            units = 3
        else:
            delta = int(getattr(event, "delta", 0))
            if delta == 0:
                return "break"
            units = -3 if delta > 0 else 3
        self.canvas.yview_scroll(units, "units")
        return "break"

    def _on_destroy_event(self, event: tk.Event) -> None:
        if event.widget is self:
            self._cancel_callbacks()

    def _cancel_callbacks(self) -> None:
        self._destroyed = True
        for after_id in tuple(self._after_ids):
            try:
                self.after_cancel(after_id)
            except tk.TclError:
                pass
        self._after_ids.clear()
        for widget in tuple(self._bound_widgets):
            try:
                for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                    widget.unbind(sequence)
            except tk.TclError:
                pass
        self._bound_widgets.clear()

    def destroy(self) -> None:
        self._cancel_callbacks()
        super().destroy()

    def scroll_to_top(self) -> None:
        self.canvas.yview_moveto(0.0)

    def scroll_to_bottom(self) -> None:
        self._sync_scrollregion()
        self.canvas.yview_moveto(1.0)
