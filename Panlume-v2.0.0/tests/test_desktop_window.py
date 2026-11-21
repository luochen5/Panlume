from __future__ import annotations

import gc
import tkinter as tk
from tkinter import ttk
import time
from datetime import datetime
from unittest.mock import patch

import pytest

from pandeng.data.schedule import CHINA_TZ
from pandeng.desktop.paths import DesktopPaths
from pandeng.desktop.settings import DesktopSettings, DesktopSettingsStore
from pandeng.desktop.widgets import ScrollablePage
from pandeng.desktop.window import PAGE_TITLES, PanlumeWindow
from test_desktop_history import populate_history, table_snapshot


@pytest.fixture
def app(tmp_path):
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"当前没有可用显示环境：{exc}")
    root.withdraw()
    window = PanlumeWindow(root, DesktopPaths(tmp_path / "personal"))
    yield window
    try:
        if root.winfo_exists():
            if window.active_tasks:
                root.destroy()
            else:
                window.close()
    except tk.TclError:
        pass
    finally:
        # Tk 对象必须在主线程回收，避免落入后续测试的后台工作线程。
        gc.collect()


def test_all_pages_render_and_fullscreen_toggles(app) -> None:
    for key in PAGE_TITLES:
        app.show_page(key)
        assert app.current_page == key
    app.toggle_fullscreen()
    assert app.fullscreen
    app.exit_fullscreen()
    assert not app.fullscreen
    assert app.data.schema_version() == "6"


def test_all_navigation_buttons_fit_minimum_window(app) -> None:
    app.root.geometry("980x640")
    app.root.deiconify()
    app.root.update()
    for button in app.nav_buttons.values():
        assert button.winfo_rooty() >= app.root.winfo_rooty()
        assert button.winfo_rooty() + button.winfo_height() <= (
            app.root.winfo_rooty() + app.root.winfo_height()
        )
    assert len(app.nav_buttons) == 12
    assert "reports" in app.nav_buttons and "results" in app.nav_buttons


def test_observation_settings_add_stock_save_and_switch_theme(app) -> None:
    app.show_page("observation")
    app.new_code_var.set("000001")
    app.new_name_var.set("平安银行")
    app._add_stock()
    app.interval_var.set("60")
    app.theme_var.set("深色")
    with patch("pandeng.desktop.window.messagebox.showinfo"):
        app._save_observation()
    saved = app.store.load()
    assert saved.theme == "dark"
    assert saved.interval_seconds == 60
    assert [item.code for item in saved.stocks] == ["002156", "000001"]


def test_model_preferences_save_without_calling_ollama(app) -> None:
    app.show_page("model")
    app.timeout_var.set("90")
    app.output_style_var.set("仅列事实")
    app.debug_prompt_var.set(True)
    with patch("pandeng.desktop.window.messagebox.showinfo"), patch(
        "pandeng.desktop.window.check_ollama"
    ) as health:
        app._save_model()
    health.assert_not_called()
    saved = app.store.load()
    assert saved.ollama_timeout_seconds == 90
    assert saved.output_style == "facts_only"
    assert saved.save_prompt_debug


def test_offline_observation_explicit_start_stop(app) -> None:
    app.show_page("observation")
    with patch("pandeng.desktop.window.datetime") as clock:
        clock.now.return_value = datetime(2026, 11, 21, tzinfo=CHINA_TZ)
        app._start_offline_observation()
    try:
        for _ in range(1000):
            app.root.update()
            time.sleep(0.01)
            snapshot = app.observation_runner.snapshot()
            if snapshot.stocks and snapshot.stocks[0].source == "simulator":
                break
        else:
            pytest.fail("离线观察没有及时显示本地模拟数据")
        assert app.observation_runner.request_start.date().isoformat() == "2026-11-20"
        app._stop_observation()
        for _ in range(300):
            app.root.update()
            time.sleep(0.01)
            if "offline-observation" not in app.active_tasks:
                break
        assert "offline-observation" not in app.active_tasks
        assert app.observation_runner.snapshot().phase == "stopped"
    finally:
        app.observation_runner.request_stop()
        for _ in range(300):
            app.root.update()
            time.sleep(0.01)
            if "offline-observation" not in app.active_tasks:
                break


def test_opening_live_acceptance_dialog_has_no_request_or_database(app) -> None:
    app.live_acceptance.executor = lambda *args, **kwargs: pytest.fail("不应发起真实请求")
    app._open_live_acceptance()
    dialogs = [item for item in app.root.winfo_children() if isinstance(item, tk.Toplevel)]
    assert len(dialogs) == 1
    app.root.update()
    dialogs[0].destroy()
    assert not app.live_acceptance.database.path.exists()
    assert not app.paths.database.with_name("akshare-live-test.db").exists()


def test_live_acceptance_dialog_actions_scroll_into_view_at_small_size(app) -> None:
    app.root.deiconify()
    app.live_acceptance.executor = lambda *args, **kwargs: pytest.fail("不应发起真实请求")
    app._open_live_acceptance()
    dialog = next(item for item in app.root.winfo_children() if isinstance(item, tk.Toplevel))
    dialog.geometry("620x520")
    app.root.update()
    host = next(item for item in dialog.winfo_children() if isinstance(item, ScrollablePage))
    assert host.body.winfo_reqheight() > host.canvas.winfo_height()

    host.scroll_to_bottom()
    app.root.update()
    assert host.canvas.yview()[1] == pytest.approx(1.0)
    buttons = [item for item in host._descendants(host.body) if isinstance(item, ttk.Button)]
    labels = {item.cget("text") for item in buttons}
    assert {"确认并执行一次真实请求", "取消"}.issubset(labels)
    actions = next(
        item.master for item in buttons if item.cget("text") == "确认并执行一次真实请求"
    )
    assert actions.winfo_rooty() >= dialog.winfo_rooty()
    assert actions.winfo_rooty() + actions.winfo_height() <= (
        dialog.winfo_rooty() + dialog.winfo_height()
    )
    dialog.destroy()
    assert not app.live_acceptance.database.path.exists()


def _wait_for_task(app, name: str) -> None:
    for _ in range(200):
        app.root.update()
        time.sleep(0.01)
        if name not in app.active_tasks:
            return
    pytest.fail(f"后台任务没有及时结束：{name}")


def test_desktop_local_review_generate_idempotent_and_read_history(app) -> None:
    app.live_acceptance.executor = lambda *args, **kwargs: pytest.fail("不应发起真实请求")
    app.show_page("review")
    app.review_code_var.set("600000")
    app.review_date_var.set("2026-01-07")

    app._generate_local_review()
    _wait_for_task(app, "local-review")

    assert "本次新建" in app.review_status_var.get()
    assert "输入 SHA-256" in app.review_meta_var.get()
    assert "数据状态 unknown" in app.review_report_text.get("1.0", "end")
    assert len(app.review_tree.get_children()) == 1

    app._generate_local_review()
    _wait_for_task(app, "local-review")
    assert "幂等返回" in app.review_status_var.get()
    assert len(app.review_tree.get_children()) == 1

    app.review_tree.selection_set("1")
    app.root.update()
    assert "历史读取" in app.review_meta_var.get()
    assert "只读取已有记录" in app.review_status_var.get()
    with app.data.database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM review_runs").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM cloud_review_calls").fetchone()[0] == 0


def test_review_page_scrolls_to_report_at_minimum_size(app) -> None:
    app.root.geometry("980x640")
    app.root.deiconify()
    app.show_page("review")
    app.root.update()
    host = app.page_hosts["review"]
    assert host.body.winfo_reqheight() > host.canvas.winfo_height()
    host.scroll_to_bottom()
    app.root.update()
    assert host.canvas.yview()[1] == pytest.approx(1.0)
    assert app.review_report_text.winfo_rooty() < app.root.winfo_rooty() + app.root.winfo_height()


def test_desktop_audit_reads_issue_and_run_details_without_writes(app) -> None:
    with app.data.database.connect() as connection:
        run_id = int(connection.execute(
            """
            INSERT INTO ingestion_runs(
                data_kind, adapter, status, received_count, accepted_count,
                duplicate_count, rejected_count, created_at
            ) VALUES ('minute', 'desktop_ui_audit', 'unknown', 1, 0, 0, 1,
                      '2026-01-12T02:00:00+00:00')
            """
        ).lastrowid)
        connection.execute(
            """
            INSERT INTO data_quality_issues(
                run_id, record_index, data_kind, stock_code, data_time,
                issue_type, severity, message, raw_json, created_at
            ) VALUES (?, 0, 'minute', '002156', '2026-01-12T09:33:00+08:00',
                      'ui_conflict', 'error', '界面质量样本', '{"raw": 1}',
                      '2026-01-12T02:00:00+00:00')
            """,
            (run_id,),
        )
    before = app.data.statistics()
    app.show_page("audit")
    app.audit_date_var.set("2026-01-12")
    app.audit_time_var.set("09:33")

    app._run_data_audit()
    _wait_for_task(app, "data-audit")

    assert "数据库写入 0 次" in app.audit_status_var.get()
    assert len(app.audit_issue_tree.get_children()) == 1
    assert app.data.statistics() == before
    issue_id = app.audit_issue_tree.get_children()[0]
    assert "ui_conflict" in app.audit_issue_tree.item(issue_id, "values")
    app.audit_issue_tree.selection_set(issue_id)
    app.root.update()
    detail = app.audit_detail_text.get("1.0", "end")
    assert "ui_conflict" in detail
    assert '"raw": 1' in detail
    app.audit_run_tree.selection_set(str(run_id))
    app.root.update()
    assert "desktop_ui_audit" in app.audit_detail_text.get("1.0", "end")


def test_audit_page_scrolls_to_trace_details_at_minimum_size(app) -> None:
    app.root.geometry("980x640")
    app.root.deiconify()
    app.show_page("audit")
    app.root.update()
    host = app.page_hosts["audit"]
    assert host.body.winfo_reqheight() > host.canvas.winfo_height()
    host.scroll_to_bottom()
    app.root.update()
    assert host.canvas.yview()[1] == pytest.approx(1.0)
    assert app.audit_detail_text.winfo_rooty() < app.root.winfo_rooty() + app.root.winfo_height()


def test_desktop_history_filters_and_reads_trace_without_writes(app) -> None:
    populate_history(app.data.database)
    before = table_snapshot(app.data.database)
    app.show_page("history")
    app.history_code_var.set("600000")
    app.history_date_var.set("2026-01-05")

    app._refresh_event_history()
    _wait_for_task(app, "event-history")

    assert len(app.history_event_tree.get_children()) == 1
    assert len(app.history_summary_tree.get_children()) == 2
    assert "模型摘要 1；模板回退 1；未完成 0" in app.history_status_var.get()
    event_id = app.history_event_tree.get_children()[0]
    app.history_event_tree.selection_set(event_id)
    app.root.update()
    assert "固定止损" in app.history_detail_text.get("1.0", "end")
    model_id = next(
        item for item in app.history_summary_tree.get_children()
        if app.history_summary_tree.item(item, "values")[6] == "model"
    )
    app.history_summary_tree.selection_set(model_id)
    app.root.update()
    detail = app.history_detail_text.get("1.0", "end")
    assert "模型整理样本" in detail
    assert "输入事实 JSON" in detail and "123 ms" in detail
    assert "请求_SECRET" not in detail and "思考_SECRET" not in detail
    assert table_snapshot(app.data.database) == before

    app.history_kind_var.set("模板回退")
    app._refresh_event_history()
    _wait_for_task(app, "event-history")
    assert len(app.history_summary_tree.get_children()) == 1
    assert table_snapshot(app.data.database) == before


def test_history_page_scrolls_to_trace_details_at_minimum_size(app) -> None:
    app.root.geometry("980x640")
    app.root.deiconify()
    app.show_page("history")
    app.root.update()
    host = app.page_hosts["history"]
    assert host.body.winfo_reqheight() > host.canvas.winfo_height()
    host.scroll_to_bottom()
    app.root.update()
    assert host.canvas.yview()[1] == pytest.approx(1.0)
    assert app.history_detail_text.winfo_rooty() < app.root.winfo_rooty() + app.root.winfo_height()


def test_model_and_data_pages_scroll_to_bottom_at_minimum_size(app) -> None:
    app.root.geometry("980x640")
    app.root.deiconify()
    for key in ("model", "data"):
        app.show_page(key)
        app.root.update()
        host = app.page_hosts[key]
        assert host.body.winfo_reqheight() > host.canvas.winfo_height()
        assert host.canvas.yview()[1] < 1.0
        host.scroll_to_bottom()
        app.root.update()
        assert host.canvas.yview()[1] == pytest.approx(1.0)


def test_integrated_demo_runs_in_background_and_scrolls_at_minimum_size(app) -> None:
    before = table_snapshot(app.data.database)
    app.root.geometry("980x640")
    app.root.deiconify()
    app.show_page("demo")
    # 前序测试的 Tk 对象由主线程回收，不能留给演示工作线程析构。
    gc.collect()
    with patch("pandeng.desktop.window.messagebox.showinfo"):
        app._run_demo()
        for _ in range(3000):
            app.root.update()
            time.sleep(0.01)
            if "analysis-demo" not in app.active_tasks:
                break
        else:
            import sys
            import threading
            import traceback
            frames = {
                thread.name: "".join(traceback.format_stack(sys._current_frames()[thread.ident]))
                for thread in threading.enumerate() if thread.ident is not None
            }
            pytest.fail(
                f"整合离线演示没有及时结束；状态 {app.status_var.get()}；"
                f"任务 {app.active_tasks}；线程栈 {frames}"
            )

    output = app.demo_text.get("1.0", "end")
    assert "发布候选离线检查" in output
    assert "固定模板摘要" in output and "本地复盘" in output
    assert "数据体检" in output and "事件与摘要" in output
    assert "模型调用 0 次" in output
    assert app.paths.demo_database.exists()
    assert table_snapshot(app.data.database) == before
    host = app.page_hosts["demo"]
    assert host.body.winfo_reqheight() > host.canvas.winfo_height()
    host.scroll_to_bottom()
    app.root.update()
    assert host.canvas.yview()[1] == pytest.approx(1.0)


def test_close_blocks_active_tasks_and_allows_idle_exit(app) -> None:
    with patch("pandeng.desktop.window.messagebox.showwarning") as warning:
        app.active_tasks.add("analysis-demo")
        app.close()
        warning.assert_called_once()
        assert app.root.winfo_exists()

    app.active_tasks.clear()
    app.close()
    assert app._closing
    assert app._ui_after_id is None
    with pytest.raises(tk.TclError):
        app.root.winfo_exists()


@pytest.fixture
def english_app(tmp_path):
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"当前没有可用显示环境：{exc}")
    root.withdraw()
    paths = DesktopPaths(tmp_path / "english-personal")
    paths.ensure_directories()
    DesktopSettingsStore(paths.settings_file).save(DesktopSettings(language="en"))
    window = PanlumeWindow(root, paths, default_language="en")
    yield window
    try:
        if root.winfo_exists():
            if window.active_tasks:
                root.destroy()
            else:
                window.close()
    except tk.TclError:
        pass
    finally:
        gc.collect()


def test_public_new_install_and_small_window_use_english(english_app) -> None:
    app = english_app
    assert app.settings.language == "en"
    assert app._ui_language == "en"
    assert app.nav_buttons["dashboard"].cget("text") == "Overview"
    assert app.nav_buttons["language"].cget("text") == "Language / 语言"
    assert app.root.title().startswith("Panlume v2.0.0")
    assert "No summary yet" in app.observation_summary_text.get("1.0", "end")
    assert app.status_var.get() == "Ready; no market data or model was requested at startup"

    app.root.geometry("980x640")
    app.root.deiconify()
    app.root.update()
    for button in app.nav_buttons.values():
        assert button.winfo_rooty() >= app.root.winfo_rooty()
        assert button.winfo_rooty() + button.winfo_height() <= (
            app.root.winfo_rooty() + app.root.winfo_height()
        )
    comboboxes = [
        item for item in app.page_hosts["observation"]._descendants(app.pages["observation"])
        if isinstance(item, ttk.Combobox)
    ]
    theme = next(item for item in comboboxes if "Light" in item["values"])
    assert tuple(theme["values"]) == ("Light", "Dark")
    assert app.output_style_var.get() == "Brief facts + data limits"
    assert app.history_kind_var.get() == "All results"


def test_language_save_waits_for_restart(app) -> None:
    app.show_page("language")
    app.language_var.set("English")
    with patch("pandeng.desktop.window.messagebox.showinfo"):
        app._save_language()

    assert app.store.load().language == "en"
    assert app.settings.language == "en"
    assert app._ui_language == "zh-CN"
    assert app.nav_buttons["dashboard"].cget("text") == "总览"
    assert app.demo.language_provider() == "zh-CN"
    assert app.review_service.language_provider() == "zh-CN"
    assert app.observation_summary.settings_provider().language == "zh-CN"
    assert "重启盘灯后生效" in app.status_var.get()

    paths = app.paths
    app.close()
    root = tk.Tk()
    root.withdraw()
    restarted = PanlumeWindow(root, paths, default_language="en")
    try:
        assert restarted._ui_language == "en"
        assert restarted.nav_buttons["dashboard"].cget("text") == "Overview"
        assert restarted.demo.language_provider() == "en"
        assert restarted.review_service.language_provider() == "en"
        assert restarted.observation_summary.settings_provider().language == "en"
        assert restarted.observation_summary_text.get("1.0", "end").startswith("No summary yet")
    finally:
        restarted.close()
        gc.collect()


def test_english_live_acceptance_dialog_is_translated_without_requests(english_app) -> None:
    app = english_app
    app.live_acceptance.executor = lambda *args, **kwargs: pytest.fail("不应发起真实请求")
    app._open_live_acceptance()
    dialog = next(item for item in app.root.winfo_children() if isinstance(item, tk.Toplevel))
    try:
        assert dialog.title() == "Prepare one live acceptance request"
        host = next(item for item in dialog.winfo_children() if isinstance(item, ScrollablePage))
        buttons = [item for item in host._descendants(host.body) if isinstance(item, ttk.Button)]
        labels = {item.cget("text") for item in buttons}
        assert {"Confirm and execute one live request", "Cancel"}.issubset(labels)
        texts = [item for item in host._descendants(host.body) if isinstance(item, tk.Text)]
        assert "No database or audit has been created yet" in texts[0].get("1.0", "end")
    finally:
        dialog.destroy()
    assert not app.live_acceptance.database.path.exists()


def test_english_history_translates_labels_but_preserves_saved_text(english_app) -> None:
    app = english_app
    populate_history(app.data.database)
    before = table_snapshot(app.data.database)
    app.show_page("history")
    app.history_code_var.set("600000")
    app.history_date_var.set("2026-01-05")
    app._refresh_event_history()
    _wait_for_task(app, "event-history")

    assert "model summaries: 1" in app.history_status_var.get()
    assert "template fallbacks: 1" in app.history_status_var.get()
    event_id = app.history_event_tree.get_children()[0]
    app.history_event_tree.selection_set(event_id)
    app.root.update()
    event_detail = app.history_detail_text.get("1.0", "end")
    assert "Latest deterministic message (original text)" in event_detail
    assert "固定止损" in event_detail

    model_id = next(
        item for item in app.history_summary_tree.get_children()
        if app.history_summary_tree.item(item, "values")[6] == "model"
    )
    app.history_summary_tree.selection_set(model_id)
    app.root.update()
    summary_detail = app.history_detail_text.get("1.0", "end")
    assert "Final saved summary (original text)" in summary_detail
    assert "模型整理样本" in summary_detail
    assert "Input facts JSON" in summary_detail
    assert table_snapshot(app.data.database) == before


def test_english_local_review_and_audit_status(english_app) -> None:
    app = english_app
    app.review_code_var.set("600000")
    app.review_date_var.set("2026-01-07")
    app._generate_local_review()
    _wait_for_task(app, "local-review")
    report = app.review_report_text.get("1.0", "end")
    assert report.startswith("Panlume local daily review")
    assert "Data status unknown" in report
    assert "created" in app.review_status_var.get()
    assert "Input SHA-256" in app.review_meta_var.get()

    app.audit_date_var.set("2026-01-07")
    app.audit_time_var.set("15:00")
    app._run_data_audit()
    _wait_for_task(app, "data-audit")
    assert "zero database writes" in app.audit_status_var.get()
    assert "Read-only data health check" in app.audit_summary_text.get("1.0", "end")



def test_watchlist_limit_enable_disable_reorder_and_save(app) -> None:
    app.show_page("observation")
    for index in range(9):
        app.new_code_var.set(f"{index + 1:06d}")
        app.new_name_var.set(f"对象{index + 1}")
        app._add_stock()
    assert len(app.stock_tree.get_children()) == 10

    app.new_code_var.set("999999")
    with patch("pandeng.desktop.window.messagebox.showerror") as error:
        app._add_stock()
    error.assert_called_once()
    assert len(app.stock_tree.get_children()) == 10

    first = app.stock_tree.get_children()[0]
    app.stock_tree.selection_set(first)
    app._toggle_stock()
    last = app.stock_tree.get_children()[-1]
    app.stock_tree.selection_set(last)
    app._move_stock(-1)
    with patch("pandeng.desktop.window.messagebox.showinfo"):
        app._save_observation()
    saved = app.store.load()
    assert len(saved.stocks) == 10
    assert saved.stocks[0].enabled is False
    assert saved.stocks[-2].code == "000009"
    assert saved.interval_seconds == 300



def test_overflow_page_has_visible_twelve_pixel_scrollbar(app) -> None:
    app.root.geometry("980x640")
    app.root.deiconify()
    app.show_page("reports")
    app.root.update()
    host = app.page_hosts["reports"]
    assert host.body.winfo_reqheight() > host.canvas.winfo_height()
    assert host.scrollbar.winfo_ismapped()
    assert host.scrollbar.winfo_width() >= 12
