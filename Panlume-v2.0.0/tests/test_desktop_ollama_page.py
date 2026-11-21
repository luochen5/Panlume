from __future__ import annotations

import gc
import threading
import time
import tkinter as tk
from unittest.mock import patch

import pytest

from pandeng.desktop.paths import DesktopPaths
from pandeng.desktop.window import PanlumeWindow
from pandeng.summarizer.management import (
    OllamaInstallation, OllamaModelInfo, PullProgress,
)
from pandeng.summarizer.ollama import OllamaError


@pytest.fixture
def app(tmp_path):
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"当前没有可用显示环境：{exc}")
    root.withdraw()
    window = PanlumeWindow(root, DesktopPaths(tmp_path / "personal"))
    yield window
    if root.winfo_exists():
        root.destroy()
    gc.collect()


def wait_for_task(app, name: str) -> None:
    for _ in range(500):
        app.root.update()
        time.sleep(0.01)
        if name not in app.active_tasks:
            return
    pytest.fail(f"后台任务没有及时结束：{name}")


def installation() -> OllamaInstallation:
    return OllamaInstallation(
        True, "C:/Tools/ollama.exe", True, "1.0",
        (OllamaModelInfo("qwen3:4b", 4_294_967_296, 4.0, "tier1", "abc", "", "Q4_K_M"),),
        "Ollama 在线，发现 1 个本地模型。",
    )


def test_detect_select_model_and_save_connection(app) -> None:
    class Client:
        def detect(self):
            return installation()

    app.show_page("model")
    app.ollama_page._client = lambda: Client()
    app.ollama_base_url_var.set("http://127.0.0.1:11435")

    app.ollama_page.detect()
    wait_for_task(app, "ollama-management-detect")

    assert app.ollama_page.tree.get_children() == ("qwen3:4b",)
    assert app.ollama_page.tree.item("qwen3:4b", "values")[1] == "4.00"
    app.ollama_page.tree.selection_set("qwen3:4b")
    app.root.update()
    assert app.model_var.get() == "qwen3:4b"
    with patch("pandeng.desktop.window.messagebox.showinfo"):
        app._save_model()
    saved = app.store.load()
    assert saved.model_name == "qwen3:4b"
    assert saved.ollama_base_url == "http://127.0.0.1:11435"


def test_download_requires_confirmation_and_uses_real_progress(app) -> None:
    class Client:
        def __init__(self) -> None:
            self.calls = 0

        def pull(self, name):
            self.calls += 1
            yield PullProgress("pulling", 50, 100, "abc")
            yield PullProgress("success", 100, 100, "abc")

    client = Client()
    app.show_page("model")
    app.ollama_page._client = lambda: client
    app.ollama_page.download_name_var.set("qwen3:4b")
    with patch("pandeng.desktop.ollama_page.messagebox.askyesno", return_value=False):
        app.ollama_page.download()
    assert client.calls == 0

    with patch("pandeng.desktop.ollama_page.messagebox.askyesno", return_value=True):
        app.ollama_page.download()
        wait_for_task(app, "ollama-download")

    assert client.calls == 1
    assert app.ollama_page.progress.cget("value") == 100
    assert "succeeded" in app.ollama_page.progress_var.get()
    assert "disabled" in app.ollama_page.cancel_button.state()


def test_cancel_and_failure_restore_controls_without_retry(app) -> None:
    class Client:
        def __init__(self, fail: bool = False) -> None:
            self.calls = 0
            self.cancelled = False
            self.started = threading.Event()
            self.release = threading.Event()
            self.fail = fail

        def pull(self, name):
            self.calls += 1
            if self.fail:
                raise OllamaError("network_error", "offline test")
            yield PullProgress("pulling", 1, 100, "abc")
            self.started.set()
            self.release.wait(timeout=2)
            if self.cancelled:
                raise OllamaError("cancelled", "cancelled by test")
            yield PullProgress("success", 100, 100, "abc")

        def cancel_download(self):
            self.cancelled = True
            self.release.set()

    for fail, expected in ((False, "cancelled"), (True, "network_error")):
        client = Client(fail)
        app.ollama_page._client = lambda current=client: current
        app.ollama_page.download_name_var.set("qwen3:4b")
        with patch("pandeng.desktop.ollama_page.messagebox.askyesno", return_value=True):
            app.ollama_page.download()
            if fail:
                wait_for_task(app, "ollama-download")
            else:
                assert client.started.wait(timeout=1)
                app.ollama_page.cancel_download()
                wait_for_task(app, "ollama-download")
        assert client.calls == 1
        assert expected in app.ollama_page.progress_var.get()
        assert "disabled" not in app.ollama_page.download_button.state()
        assert "disabled" in app.ollama_page.cancel_button.state()


def test_delete_requires_selection_and_second_confirmation(app) -> None:
    class Client:
        def __init__(self) -> None:
            self.deleted = []

        def detect(self):
            return installation()

        def delete_model(self, name):
            self.deleted.append(name)

    client = Client()
    app.ollama_page._client = lambda: client
    app.ollama_page.detect()
    wait_for_task(app, "ollama-management-detect")
    with patch("pandeng.desktop.ollama_page.messagebox.showwarning"), patch(
        "pandeng.desktop.ollama_page.messagebox.askyesno", return_value=False
    ):
        app.ollama_page.delete_selected()
    assert client.deleted == []

    app.ollama_page.tree.selection_set("qwen3:4b")
    with patch("pandeng.desktop.ollama_page.messagebox.askyesno", return_value=True):
        app.ollama_page.delete_selected()
        wait_for_task(app, "ollama-delete")
    assert client.deleted == ["qwen3:4b"]
    assert "deleted" in app.ollama_page.progress_var.get()
