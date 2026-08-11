"""Contract tests for the Stagehand v4 browser edge adapter."""

from __future__ import annotations

from unittest.mock import patch

from hermes_cli.config import DEFAULT_CONFIG
from tools import browser_tool, stagehand_driver


def test_stagehand_is_default_for_new_configs() -> None:
    browser = DEFAULT_CONFIG["browser"]
    assert browser["driver"] == "stagehand"
    assert browser["stagehand"]["model"].startswith("openai/")
    assert browser["stagehand"]["fallback_to_agent_browser"] is True


def test_sidecar_support_matrix_preserves_annotated_fallback() -> None:
    assert stagehand_driver.supports("open", ["https://example.com"])
    assert stagehand_driver.supports("snapshot", [])
    assert stagehand_driver.supports("screenshot", ["--full"])
    assert not stagehand_driver.supports("screenshot", ["--annotate"])
    assert not stagehand_driver.supports("record", ["start"])


def test_dispatches_existing_command_envelope_to_stagehand(monkeypatch) -> None:
    request = {}
    monkeypatch.setattr(stagehand_driver, "package_available", lambda: True)
    monkeypatch.setattr(stagehand_driver, "supports", lambda command, args: True)
    monkeypatch.setattr(browser_tool, "_get_session_info", lambda task: {
        "session_name": "test", "cdp_url": "ws://browser.test/devtools/browser/1"
    })
    monkeypatch.setattr(browser_tool, "_build_browser_env", lambda: {"PATH": "/usr/bin"})
    monkeypatch.setattr(browser_tool, "_merge_browser_path", lambda value: value)
    monkeypatch.setattr(browser_tool, "_is_headed_mode", lambda: False)

    def fake_request(session_key, command, args, config, env, timeout):
        request.update(locals())
        return {"success": True, "data": {"url": args[0], "title": "Example"}}

    monkeypatch.setattr(stagehand_driver, "request", fake_request)
    with patch("hermes_cli.config.read_raw_config", return_value={
        "browser": {"driver": "stagehand", "stagehand": {"model": "openai/gpt-5-mini"}}
    }):
        result = browser_tool._run_browser_command(
            "task-1", "open", ["https://example.com"], timeout=12
        )

    assert result["success"] is True
    assert request["session_key"] == "task-1"
    assert request["command"] == "open"
    assert request["config"]["backend"] == "cdp"
    assert request["config"]["cdpUrl"].startswith("ws://")
    assert request["config"]["model"].startswith("openai/")


def test_explicit_legacy_driver_skips_stagehand(monkeypatch) -> None:
    monkeypatch.setattr(stagehand_driver, "request", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("Stagehand must not run")
    ))
    with patch("hermes_cli.config.read_raw_config", return_value={
        "browser": {"driver": "agent-browser"}
    }), patch.object(browser_tool, "_find_agent_browser", return_value="/missing/agent-browser"), \
         patch.object(browser_tool, "_requires_real_termux_browser_install", return_value=False), \
         patch.object(browser_tool, "_is_local_mode", return_value=True), \
         patch.object(browser_tool, "_chromium_installed", return_value=False), \
         patch.object(browser_tool, "_get_browser_engine", return_value="auto"), \
         patch.object(browser_tool, "_maybe_autoinstall_chromium", return_value=False):
        result = browser_tool._run_browser_command("task-1", "snapshot", [], timeout=1)
    assert result["success"] is False


def test_cleanup_reaps_stagehand_even_without_active_browser_sessions(monkeypatch) -> None:
    called = []
    monkeypatch.setattr(stagehand_driver, "close_all", lambda: called.append(True))
    monkeypatch.setattr(browser_tool, "_active_sessions", {})
    browser_tool.cleanup_all_browsers()
    assert called == [True]


def test_requirements_accept_stagehand_without_agent_browser(monkeypatch) -> None:
    monkeypatch.setattr(stagehand_driver, "package_available", lambda: True)
    monkeypatch.setattr(browser_tool, "_get_cloud_provider", lambda: None)
    monkeypatch.setattr(browser_tool, "_find_agent_browser", lambda **kwargs: (_ for _ in ()).throw(
        AssertionError("legacy CLI discovery must not run")
    ))
    with patch("hermes_cli.config.read_raw_config", return_value={
        "browser": {"driver": "stagehand"}
    }):
        assert browser_tool.check_browser_requirements() is True