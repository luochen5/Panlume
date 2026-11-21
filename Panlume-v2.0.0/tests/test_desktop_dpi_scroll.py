from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import pandeng.desktop.dpi as dpi
from pandeng.desktop.theme import DesktopTheme
from pandeng.desktop.widgets import ScrollablePage
from pandeng.desktop.window import main


def test_per_monitor_v2_is_single_process_entry(monkeypatch) -> None:
    monkeypatch.setattr(dpi.os, "name", "nt")
    monkeypatch.setattr(dpi, "_dpi_initialized", False)
    with patch("ctypes.windll.user32.SetProcessDpiAwarenessContext", return_value=True, create=True) as configure:
        assert dpi.configure_windows_dpi() is True
        assert dpi.configure_windows_dpi() is False
    configure.assert_called_once()


def test_main_configures_dpi_before_root_and_scaling_before_window() -> None:
    events = []
    root = SimpleNamespace(mainloop=lambda: events.append("mainloop"))
    with patch("pandeng.desktop.window.configure_windows_dpi", side_effect=lambda: events.append("dpi")), patch(
        "pandeng.desktop.window.tk.Tk", side_effect=lambda: events.append("root") or root
    ), patch(
        "pandeng.desktop.window.initialize_tk_scaling", side_effect=lambda current: events.append("scaling")
    ), patch(
        "pandeng.desktop.window.PanlumeWindow", side_effect=lambda *args, **kwargs: events.append("window")
    ):
        assert main() == 0
    assert events == ["dpi", "root", "scaling", "window", "mainloop"]


def test_tk_scaling_initializes_once_per_root() -> None:
    calls = []
    root = SimpleNamespace(
        winfo_fpixels=lambda _value: 120.0,
        tk=SimpleNamespace(call=lambda *args: calls.append(args) or 1.5),
    )
    first = dpi.initialize_tk_scaling(root)
    second = dpi.initialize_tk_scaling(root)
    assert first == pytest.approx(120 / 72)
    assert second == 1.5
    assert calls.count(("tk", "scaling", 120 / 72)) == 1


def test_scroll_bindings_are_deduplicated_and_text_keeps_own_behavior() -> None:
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"当前没有可用显示环境：{exc}")
    page = ScrollablePage(root, background="white")
    text = tk.Text(page.body)
    text.pack()
    try:
        page.bind_mousewheel()
        count = len(page._bound_widgets)
        page.bind_mousewheel()
        assert len(page._bound_widgets) == count
        assert text not in page._bound_widgets
        event = SimpleNamespace(delta=120, num=None)
        assert page._on_mousewheel(event) == "break"
    finally:
        root.destroy()


def test_destroy_cancels_idle_callbacks_and_unbinds_widgets() -> None:
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"当前没有可用显示环境：{exc}")
    page = ScrollablePage(root, background="white")
    page.bind_mousewheel()
    called = []
    page._schedule_idle(lambda: called.append("ran"))
    page.destroy()
    root.update()
    assert called == []
    assert page._after_ids == set()
    assert page._bound_widgets == set()
    root.destroy()



def test_scrollbar_slider_is_flat_slim_and_has_no_legacy_arrows() -> None:
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"当前没有可用显示环境：{exc}")
    try:
        for theme_name in ("light", "dark"):
            theme = DesktopTheme(root, theme_name)
            style = ttk.Style(root)
            assert "arrow" not in str(style.layout("Vertical.TScrollbar")).lower()
            assert "arrow" not in str(style.layout("Horizontal.TScrollbar")).lower()
            assert int(style.configure("TScrollbar", "width")) == 12
            assert style.configure("TScrollbar", "relief") == "flat"
            assert style.configure("TScrollbar", "troughcolor") == theme.colors["bg"]
    finally:
        root.destroy()
