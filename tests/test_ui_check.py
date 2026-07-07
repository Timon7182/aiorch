#!/usr/bin/env python3
"""
Tests for the on-demand UI-check feature
========================================

Covers:
- core.mcp_secret_proxy: placeholder substitution + redaction (JSON-aware)
- server.services.ui_check_service: target URL + credentials resolution
- prompts_pkg.get_ui_check_prompt: parameter assembly and gating
- cli.ui_check_commands: verdict reading + BLOCKED fallback contract
- agents.tools_pkg: ui_checker agent config + playwright forcing
- server.routes.tasks: ui-check report endpoint + status derivation
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "backend"))
sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "web-server"))

from core.mcp_secret_proxy import (
    load_secrets,
    redact_bytes,
    redact_line,
    substitute_line,
)


# =============================================================================
# SECRET PROXY
# =============================================================================


class TestSecretProxy:
    SECRETS = {
        "${UI_CHECK_USERNAME}": "qa-admin",
        "${UI_CHECK_PASSWORD}": 'p@ss"w\\ord',  # quote + backslash: JSON-escaping matters
    }

    def test_load_secrets_from_env(self):
        env = {
            "MCP_PROXY_SECRET_VARS": "UI_CHECK_USERNAME, UI_CHECK_PASSWORD, MISSING",
            "UI_CHECK_USERNAME": "u",
            "UI_CHECK_PASSWORD": "p",
        }
        secrets = load_secrets(env)
        assert secrets == {"${UI_CHECK_USERNAME}": "u", "${UI_CHECK_PASSWORD}": "p"}

    def test_load_secrets_empty(self):
        assert load_secrets({}) == {}
        assert load_secrets({"MCP_PROXY_SECRET_VARS": ""}) == {}

    def test_substitute_simple(self):
        msg = json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "browser_fill_form", "arguments": {
                "fields": [{"name": "password", "value": "${UI_CHECK_PASSWORD}"}]
            }},
        })
        out = substitute_line(msg, self.SECRETS)
        decoded = json.loads(out)
        value = decoded["params"]["arguments"]["fields"][0]["value"]
        assert value == 'p@ss"w\\ord'

    def test_substitute_inside_longer_string(self):
        msg = json.dumps({"params": {"text": "user=${UI_CHECK_USERNAME}!"}})
        out = json.loads(substitute_line(msg, self.SECRETS))
        assert out["params"]["text"] == "user=qa-admin!"

    def test_substitute_no_placeholder_is_passthrough(self):
        msg = json.dumps({"params": {"text": "nothing here"}})
        assert substitute_line(msg, self.SECRETS) == msg

    def test_substitute_never_touches_keys(self):
        msg = json.dumps({"${UI_CHECK_USERNAME}": "key stays"})
        out = json.loads(substitute_line(msg, self.SECRETS))
        assert "${UI_CHECK_USERNAME}" in out

    def test_substitute_invalid_json_passthrough(self):
        line = "not json ${UI_CHECK_PASSWORD}"
        assert substitute_line(line, self.SECRETS) == line

    def test_redact_value_in_json(self):
        msg = json.dumps({"result": {"content": [{"type": "text", "text": 'field shows p@ss"w\\ord here'}]}})
        out = redact_line(msg, self.SECRETS)
        assert 'p@ss' not in out or '${UI_CHECK_PASSWORD}' in out
        decoded = json.loads(out)
        assert decoded["result"]["content"][0]["text"] == "field shows ${UI_CHECK_PASSWORD} here"

    def test_redact_plain_text_line(self):
        line = "server log: password=qa-admin done"
        out = redact_line(line, {"${U}": "qa-admin"})
        assert "qa-admin" not in out
        assert "${U}" in out

    def test_redact_no_secret_passthrough(self):
        msg = json.dumps({"result": "clean"})
        assert redact_line(msg, self.SECRETS) == msg

    def test_roundtrip(self):
        """substitute → redact restores the placeholder (no leak either way)."""
        msg = json.dumps({"params": {"value": "${UI_CHECK_PASSWORD}"}})
        substituted = substitute_line(msg, self.SECRETS)
        assert "${UI_CHECK_PASSWORD}" not in substituted
        restored = redact_line(substituted, self.SECRETS)
        assert json.loads(restored)["params"]["value"] == "${UI_CHECK_PASSWORD}"

    def test_empty_secrets_noop(self):
        msg = json.dumps({"a": "${X}"})
        assert substitute_line(msg, {}) == msg
        assert redact_line(msg, {}) == msg

    def test_redact_bytes_binary_fallback(self):
        """Non-UTF-8 chunks must still be redacted at the byte level."""
        raw = b"\xff\xfe prefix qa-admin suffix \xff"
        out = redact_bytes(raw, {"${U}": "qa-admin"})
        assert b"qa-admin" not in out
        assert b"${U}" in out
        # untouched when no secret present
        assert redact_bytes(b"\xff\xfe clean", {"${U}": "qa-admin"}) == b"\xff\xfe clean"

    @pytest.mark.integration
    def test_proxy_subprocess_end_to_end(self):
        """Spawn the real proxy around an echo server: the placeholder must be
        substituted on the way in (server sees the real value) and redacted on
        the way out (client sees the placeholder again)."""
        import subprocess

        proxy_script = (
            Path(__file__).parent.parent
            / "apps" / "backend" / "core" / "mcp_secret_proxy.py"
        )
        # Echo server that wraps each incoming line in a JSON result.
        echo_server = (
            "import sys, json\n"
            "for line in sys.stdin:\n"
            "    msg = json.loads(line)\n"
            "    val = msg['params']['value']\n"
            "    print(json.dumps({'result': {'echoed': val, 'raw_ok': val == 'real-secret'}}), flush=True)\n"
        )
        env = {
            **__import__('os').environ,
            "MCP_PROXY_SECRET_VARS": "TEST_SECRET",
            "TEST_SECRET": "real-secret",
        }
        proc = subprocess.Popen(
            [sys.executable, str(proxy_script), "--", sys.executable, "-c", echo_server],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env,
        )
        try:
            request = json.dumps({"params": {"value": "${TEST_SECRET}"}}) + "\n"
            proc.stdin.write(request.encode())
            proc.stdin.flush()
            response = json.loads(proc.stdout.readline())
            # Server received the REAL value (raw_ok computed server-side)...
            assert response["result"]["raw_ok"] is True
            # ...but the client-visible echo is redacted back to the placeholder.
            assert response["result"]["echoed"] == "${TEST_SECRET}"
        finally:
            proc.stdin.close()
            proc.wait(timeout=10)


# =============================================================================
# UI CHECK SERVICE (web-server)
# =============================================================================

ui_check_service = pytest.importorskip(
    "server.services.ui_check_service",
    reason="web-server deps not installed",
)


class TestTargetUrlValidation:
    def test_valid(self):
        assert ui_check_service.is_valid_target_url("http://192.168.88.55:3100")
        assert ui_check_service.is_valid_target_url("https://app.example.com/path")

    def test_invalid(self):
        assert not ui_check_service.is_valid_target_url(None)
        assert not ui_check_service.is_valid_target_url("")
        assert not ui_check_service.is_valid_target_url("file:///etc/passwd")
        assert not ui_check_service.is_valid_target_url("javascript:alert(1)")
        assert not ui_check_service.is_valid_target_url("ftp://host/x")
        assert not ui_check_service.is_valid_target_url("http://")  # no host


class TestResolveTarget:
    def _project_with_envs(self, tmp_path: Path) -> Path:
        (tmp_path / "deploy.config.json").write_text(json.dumps({
            "strategy": "dev-server",
            "environments": {
                "test": {"url": "http://test.local:3100", "credsPrefix": "UI_CHECK_TEST"},
                "bad": {"url": "file:///nope"},
            },
        }))
        return tmp_path

    def test_direct_url_wins(self, tmp_path):
        url, entry = ui_check_service.resolve_ui_check_target(
            self._project_with_envs(tmp_path),
            {"url": "http://direct:1234", "environment": "test"},
        )
        assert url == "http://direct:1234"
        # environment entry still resolved (for creds prefix)
        assert entry.get("credsPrefix") == "UI_CHECK_TEST"

    def test_named_environment(self, tmp_path):
        url, entry = ui_check_service.resolve_ui_check_target(
            self._project_with_envs(tmp_path), {"environment": "test"}
        )
        assert url == "http://test.local:3100"
        assert entry["credsPrefix"] == "UI_CHECK_TEST"

    def test_invalid_scheme_rejected_falls_to_preview(self, tmp_path):
        url, _ = ui_check_service.resolve_ui_check_target(
            self._project_with_envs(tmp_path),
            {"environment": "bad"},
            preview_url="http://preview:5000",
        )
        assert url == "http://preview:5000"

    def test_preview_fallback(self, tmp_path):
        url, _ = ui_check_service.resolve_ui_check_target(
            tmp_path, {}, preview_url="http://preview:5000"
        )
        assert url == "http://preview:5000"

    def test_nothing_resolves_to_none(self, tmp_path):
        url, entry = ui_check_service.resolve_ui_check_target(tmp_path, {})
        assert url is None
        assert entry == {}

    def test_unknown_environment_name(self, tmp_path):
        url, entry = ui_check_service.resolve_ui_check_target(
            self._project_with_envs(tmp_path), {"environment": "nope"}
        )
        assert url is None
        assert entry == {}


class TestResolveCredentials:
    def test_generic_pair(self):
        env = {"UI_CHECK_USERNAME": "u", "UI_CHECK_PASSWORD": "p"}
        creds = ui_check_service.resolve_ui_check_credentials(env, {})
        assert creds["UI_CHECK_USERNAME"] == "u"
        assert creds["UI_CHECK_PASSWORD"] == "p"
        assert creds["UI_CHECK_SECRET_VARS"] == "UI_CHECK_USERNAME,UI_CHECK_PASSWORD"

    def test_password_only(self):
        env = {"UI_CHECK_PASSWORD": "p"}
        creds = ui_check_service.resolve_ui_check_credentials(env, {})
        assert creds["UI_CHECK_SECRET_VARS"] == "UI_CHECK_PASSWORD"
        assert "UI_CHECK_USERNAME" not in creds

    def test_no_creds(self):
        assert ui_check_service.resolve_ui_check_credentials({}, {}) == {}
        # username without password does not count
        assert ui_check_service.resolve_ui_check_credentials(
            {"UI_CHECK_USERNAME": "u"}, {}
        ) == {}

    def test_env_prefix_beats_generic(self):
        env = {
            "UI_CHECK_USERNAME": "generic", "UI_CHECK_PASSWORD": "generic-pw",
            "UI_CHECK_TEST_USERNAME": "test-u", "UI_CHECK_TEST_PASSWORD": "test-pw",
        }
        creds = ui_check_service.resolve_ui_check_credentials(
            env, {}, {"credsPrefix": "UI_CHECK_TEST"}
        )
        assert creds["UI_CHECK_USERNAME"] == "test-u"
        assert creds["UI_CHECK_PASSWORD"] == "test-pw"

    def test_role_specific_beats_env_prefix(self):
        env = {
            "UI_CHECK_TEST_PASSWORD": "plain",
            "UI_CHECK_TEST_ADMIN_USERNAME": "root",
            "UI_CHECK_TEST_ADMIN_PASSWORD": "root-pw",
        }
        creds = ui_check_service.resolve_ui_check_credentials(
            env, {"role": "Admin"}, {"credsPrefix": "UI_CHECK_TEST"}
        )
        assert creds["UI_CHECK_USERNAME"] == "root"
        assert creds["UI_CHECK_PASSWORD"] == "root-pw"

    def test_role_token_sanitized(self):
        env = {"UI_CHECK_QA_LEAD_PASSWORD": "pw"}
        creds = ui_check_service.resolve_ui_check_credentials(env, {"role": "QA lead"})
        assert creds["UI_CHECK_PASSWORD"] == "pw"


# =============================================================================
# PROMPT ASSEMBLY (backend)
# =============================================================================


class TestUiCheckPrompt:
    def _spec_dir(self, tmp_path: Path, ui_check: dict | None = None) -> Path:
        spec_dir = tmp_path / "specs" / "001-check"
        spec_dir.mkdir(parents=True)
        meta = {"taskType": "ui_check"}
        if ui_check is not None:
            meta["uiCheck"] = ui_check
        (spec_dir / "task_metadata.json").write_text(json.dumps(meta))
        (spec_dir / "requirements.json").write_text(json.dumps({
            "title": "Check task creation",
            "description": "Verify the wizard creates a task",
        }))
        return spec_dir

    def test_prompt_includes_params_and_protocol(self, tmp_path, monkeypatch):
        monkeypatch.delenv("UI_CHECK_TARGET_URL", raising=False)
        monkeypatch.delenv("UI_CHECK_SECRET_VARS", raising=False)
        from prompts_pkg import get_ui_check_prompt

        spec_dir = self._spec_dir(tmp_path, {
            "url": "http://target:3100",
            "role": "admin",
            "steps": "1. Open wizard",
            "expected": "Task appears",
            "attempts": 2,
        })
        prompt = get_ui_check_prompt(spec_dir, tmp_path)
        assert "http://target:3100" in prompt
        assert "admin" in prompt
        assert "1. Open wizard" in prompt
        assert "Task appears" in prompt
        assert "Attempts requested:** 2" in prompt
        assert "UI CHECK PROTOCOL" in prompt
        assert "ui_check_report.md" in prompt
        # no creds configured → the "credentials: none" note
        assert "none configured" in prompt

    def test_env_target_url_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv("UI_CHECK_TARGET_URL", "http://resolved:9999")
        from prompts_pkg import get_ui_check_prompt

        spec_dir = self._spec_dir(tmp_path, {"url": "http://metadata:1111"})
        prompt = get_ui_check_prompt(spec_dir, tmp_path)
        assert "http://resolved:9999" in prompt

    def test_missing_url_marks_blocked(self, tmp_path, monkeypatch):
        monkeypatch.delenv("UI_CHECK_TARGET_URL", raising=False)
        from prompts_pkg import get_ui_check_prompt

        prompt = get_ui_check_prompt(self._spec_dir(tmp_path, {}), tmp_path)
        assert "NOT PROVIDED" in prompt

    def test_non_http_metadata_url_rejected(self, tmp_path, monkeypatch):
        """file:///data:/javascript: URLs from task metadata must never reach
        the prompt (the liveness probe curls the target)."""
        monkeypatch.delenv("UI_CHECK_TARGET_URL", raising=False)
        from prompts_pkg import get_ui_check_prompt

        for bad in ("file:///etc/passwd", "javascript:alert(1)", "data:text/html,x"):
            prompt = get_ui_check_prompt(
                self._spec_dir(tmp_path / bad.split(":")[0], {"url": bad}), tmp_path
            )
            assert bad not in prompt
            assert "NOT PROVIDED" in prompt

    def test_secret_placeholders_listed(self, tmp_path, monkeypatch):
        monkeypatch.setenv("UI_CHECK_TARGET_URL", "http://t:1")
        monkeypatch.setenv(
            "UI_CHECK_SECRET_VARS", "UI_CHECK_USERNAME,UI_CHECK_PASSWORD"
        )
        from prompts_pkg import get_ui_check_prompt

        prompt = get_ui_check_prompt(self._spec_dir(tmp_path, {}), tmp_path)
        assert "${UI_CHECK_USERNAME}" in prompt
        assert "${UI_CHECK_PASSWORD}" in prompt

    def test_attempts_clamped(self, tmp_path, monkeypatch):
        monkeypatch.delenv("UI_CHECK_TARGET_URL", raising=False)
        from prompts_pkg import get_ui_check_prompt

        prompt = get_ui_check_prompt(
            self._spec_dir(tmp_path, {"attempts": 99}), tmp_path
        )
        assert "Attempts requested:** 3" in prompt

    def test_bug_repro_context_not_injected_for_ui_check(self, tmp_path):
        from prompts_pkg.prompts import _get_bug_repro_context

        spec_dir = self._spec_dir(tmp_path, {})
        assert _get_bug_repro_context(spec_dir, "reviewer") == ""


# =============================================================================
# CLI RUNNER CONTRACT (backend)
# =============================================================================


class TestUiCheckRunnerContract:
    def test_read_verdict_valid(self, tmp_path):
        from cli.ui_check_commands import read_ui_check_verdict

        (tmp_path / "ui_check_result.json").write_text(json.dumps({"verdict": "pass"}))
        assert read_ui_check_verdict(tmp_path) == "PASS"

    def test_read_verdict_invalid_or_missing(self, tmp_path):
        from cli.ui_check_commands import read_ui_check_verdict

        assert read_ui_check_verdict(tmp_path) is None
        (tmp_path / "ui_check_result.json").write_text(json.dumps({"verdict": "MAYBE"}))
        assert read_ui_check_verdict(tmp_path) is None
        (tmp_path / "ui_check_result.json").write_text("not json")
        assert read_ui_check_verdict(tmp_path) is None

    def test_blocked_fallback_writes_contract_files(self, tmp_path):
        from cli.ui_check_commands import read_ui_check_verdict, write_blocked_fallback

        write_blocked_fallback(tmp_path, "browser did not start")
        report = (tmp_path / "ui_check_report.md").read_text(encoding="utf-8")
        assert report.startswith("# UI Check Report")
        assert "## Verdict" in report
        assert "BLOCKED" in report
        assert "browser did not start" in report
        result = json.loads((tmp_path / "ui_check_result.json").read_text())
        assert result["verdict"] == "BLOCKED"
        assert result["written_by"] == "runner_fallback"
        assert read_ui_check_verdict(tmp_path) == "BLOCKED"


class TestUiCheckRunnerSession:
    """End-to-end of run_ui_check_session with a faked SDK client."""

    def _spec_dir(self, tmp_path: Path) -> Path:
        spec_dir = tmp_path / ".magestic-ai" / "specs" / "001-check"
        spec_dir.mkdir(parents=True)
        (spec_dir / "task_metadata.json").write_text(json.dumps({
            "taskType": "ui_check",
            "uiCheck": {"url": "http://t:1", "steps": "1. open"},
        }))
        (spec_dir / "requirements.json").write_text(json.dumps({
            "title": "t", "description": "d",
        }))
        (spec_dir / "spec.md").write_text("# t\n")
        return spec_dir

    def _fake_client(self, on_query=None):
        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def query(self, prompt):
                if on_query:
                    on_query(prompt)

            async def receive_response(self):
                if False:  # pragma: no cover - empty async generator
                    yield None

        return FakeClient()

    async def test_agent_writes_result(self, tmp_path, monkeypatch):
        import cli.ui_check_commands as uic

        spec_dir = self._spec_dir(tmp_path)

        def write_result(prompt):
            # Simulate the agent doing its job during the session.
            (spec_dir / "ui_check_report.md").write_text("# UI Check Report\n## Verdict\nPASS\n")
            (spec_dir / "ui_check_result.json").write_text(json.dumps({"verdict": "PASS"}))

        monkeypatch.setattr(
            uic, "create_client", lambda *a, **k: self._fake_client(write_result)
        )
        verdict = await uic.run_ui_check_session(tmp_path, spec_dir, None)
        assert verdict == "PASS"

    async def test_agent_writes_nothing_gets_blocked_fallback(self, tmp_path, monkeypatch):
        import cli.ui_check_commands as uic

        spec_dir = self._spec_dir(tmp_path)
        monkeypatch.setattr(uic, "create_client", lambda *a, **k: self._fake_client())
        verdict = await uic.run_ui_check_session(tmp_path, spec_dir, None)
        assert verdict == "BLOCKED"
        assert (spec_dir / "ui_check_report.md").exists()
        result = json.loads((spec_dir / "ui_check_result.json").read_text())
        assert result["written_by"] == "runner_fallback"

    async def test_session_exception_gets_blocked_fallback(self, tmp_path, monkeypatch):
        import cli.ui_check_commands as uic

        spec_dir = self._spec_dir(tmp_path)

        def boom(*a, **k):
            raise RuntimeError("MCP server failed to start")

        monkeypatch.setattr(uic, "create_client", boom)
        verdict = await uic.run_ui_check_session(tmp_path, spec_dir, None)
        assert verdict == "BLOCKED"
        report = (spec_dir / "ui_check_report.md").read_text(encoding="utf-8")
        assert "MCP server failed to start" in report


# =============================================================================
# AGENT CONFIG (backend)
# =============================================================================


class TestUiCheckerAgentConfig:
    def test_ui_checker_registered(self):
        from agents.tools_pkg.models import AGENT_CONFIGS, get_agent_config

        assert "ui_checker" in AGENT_CONFIGS
        config = get_agent_config("ui_checker")
        assert "browser" in config["mcp_servers"]
        assert "magestic-ai" in config["mcp_servers"]

    def test_playwright_tools_include_network_requests(self):
        from agents.tools_pkg.models import PLAYWRIGHT_TOOLS

        assert "mcp__playwright__browser_network_requests" in PLAYWRIGHT_TOOLS

    def test_playwright_forced_by_add_override(self):
        from agents.tools_pkg.models import get_required_mcp_servers

        servers = get_required_mcp_servers(
            "ui_checker",
            project_capabilities={"is_web_frontend": False},
            mcp_config={"AGENT_MCP_ui_checker_ADD": "playwright"},
        )
        assert "playwright" in servers

    def test_playwright_not_started_without_gates(self):
        from agents.tools_pkg.models import get_required_mcp_servers

        servers = get_required_mcp_servers(
            "ui_checker",
            project_capabilities={"is_web_frontend": False},
            mcp_config={},
        )
        assert "playwright" not in servers

    def test_allowed_tools_include_playwright_when_forced(self):
        from agents.tools_pkg.permissions import get_allowed_tools

        tools = get_allowed_tools(
            "ui_checker",
            project_capabilities={"is_web_frontend": True},
            mcp_config={"AGENT_MCP_ui_checker_ADD": "playwright"},
        )
        assert "mcp__playwright__browser_navigate" in tools
        assert "mcp__playwright__browser_network_requests" in tools
        assert "Write" in tools


# =============================================================================
# REPORT ENDPOINT + STATUS DERIVATION (web-server)
# =============================================================================

tasks_routes = pytest.importorskip(
    "server.routes.tasks", reason="web-server deps not installed"
)


class TestUiCheckReportEndpoint:
    def _spec(self, tmp_path: Path) -> tuple[Path, Path]:
        project = tmp_path / "proj"
        spec_dir = project / ".magestic-ai" / "specs" / "001-check"
        spec_dir.mkdir(parents=True)
        return project, spec_dir

    async def test_report_found(self, tmp_path, monkeypatch):
        project, spec_dir = self._spec(tmp_path)
        (spec_dir / "ui_check_report.md").write_text("# UI Check Report\n\n## Verdict\nPASS\n")
        (spec_dir / "ui_check_result.json").write_text(json.dumps({"verdict": "PASS"}))
        evidence = spec_dir / "evidence-ui-check"
        evidence.mkdir()
        (evidence / "step-1.png").write_bytes(b"\x89PNG")
        (evidence / "notes.txt").write_text("not an image")

        monkeypatch.setattr(
            tasks_routes, "_resolve_task",
            lambda task_id: ("p1", "001-check", project, spec_dir),
        )
        result = await tasks_routes.get_ui_check_report("p1:001-check")
        assert result["exists"] is True
        assert result["verdict"] == "PASS"
        # lowercase verdicts are normalised for the frontend pill lookup
        (spec_dir / "ui_check_result.json").write_text(json.dumps({"verdict": "blocked"}))
        result = await tasks_routes.get_ui_check_report("p1:001-check")
        assert result["verdict"] == "BLOCKED"
        assert "# UI Check Report" in result["content"]
        assert result["evidence"] == [
            {"name": "step-1.png", "path": "evidence-ui-check/step-1.png"}
        ]

    async def test_report_missing(self, tmp_path, monkeypatch):
        project, spec_dir = self._spec(tmp_path)
        monkeypatch.setattr(
            tasks_routes, "_resolve_task",
            lambda task_id: ("p1", "001-check", project, spec_dir),
        )
        result = await tasks_routes.get_ui_check_report("p1:001-check")
        assert result == {"exists": False, "verdict": None, "content": None, "evidence": []}


class TestUiCheckStatusDerivation:
    def test_done_when_result_exists(self, tmp_path):
        spec_dir = tmp_path / "001-check"
        spec_dir.mkdir()
        (spec_dir / "task_metadata.json").write_text(json.dumps({"taskType": "ui_check"}))
        (spec_dir / "requirements.json").write_text(json.dumps({
            "title": "t", "description": "d", "metadata": {"taskType": "ui_check"},
        }))
        (spec_dir / "ui_check_result.json").write_text(json.dumps({"verdict": "FAIL"}))
        metadata = tasks_routes.load_spec_metadata(spec_dir)
        assert metadata["status"] == "done"

    def test_backlog_before_run(self, tmp_path):
        spec_dir = tmp_path / "001-check"
        spec_dir.mkdir()
        (spec_dir / "task_metadata.json").write_text(json.dumps({"taskType": "ui_check"}))
        (spec_dir / "requirements.json").write_text(json.dumps({"title": "t", "description": "d"}))
        metadata = tasks_routes.load_spec_metadata(spec_dir)
        assert metadata["status"] == "backlog"
