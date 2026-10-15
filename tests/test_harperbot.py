# SPDX-License-Identifier: MIT
# Copyright (c) 2026 coccinella-labs

#!/usr/bin/env python3
"""
Unit tests for HarperBot core functionality.
Run with: python -m pytest test/test_harperbot.py
"""

import os
import unittest
from unittest.mock import Mock, patch

from harperbot.harperbot import (  # noqa: E402
    ANALYSIS_MARKER,
    BotInternalError,
    analyze_with_gemini,
    apply_suggestions_to_pr,
    create_branch,
    find_diff_position,
    load_config,
    find_existing_analysis_comments,
    classify_provider_error,
    parse_diff_for_suggestions,
    upsert_analysis_comment,
    verify_webhook_signature,
    workflow_run_url,
)


class TestHarperBot(unittest.TestCase):
    """Test cases for HarperBot functionality."""

    def test_verify_webhook_signature_valid(self):
        """Test webhook signature verification with valid signature."""
        payload = b'{"test": "data"}'
        secret = "test-secret"
        import hashlib
        import hmac

        expected_sig = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()

        result = verify_webhook_signature(payload, expected_sig, secret)
        self.assertTrue(result)

    def test_verify_webhook_signature_invalid(self):
        """Test webhook signature verification with invalid signature."""
        payload = b'{"test": "data"}'
        secret = "test-secret"
        invalid_sig = "sha256=invalid"

        result = verify_webhook_signature(payload, invalid_sig, secret)
        self.assertFalse(result)

    def test_load_config_defaults(self):
        """Test loading config with defaults when no config file exists."""
        with patch("os.path.exists", return_value=False):
            config = load_config()
            self.assertEqual(config["focus"], "all")
            self.assertEqual(config["model"], "gemini-3.8-flash")
            self.assertEqual(config["max_diff_length"], 4000)
            self.assertEqual(config["temperature"], 0.2)
            self.assertEqual(config["max_output_tokens"], 4096)
            self.assertIn("prompt", config)  # Should include default prompt

    @patch("harperbot.harperbot.load_config")
    @patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"})
    def test_analyze_with_gemini_success(self, mock_load_config):
        # Mock config with all required keys
        mock_load_config.return_value = {
            "model": "gemini-2.0-flash",
            "focus": "all",
            "max_diff_length": 4000,
            "temperature": 0.2,
            "max_output_tokens": 4096,
            "prompt": "Test prompt {num_files} {files_list} {diff_content} {focus_instruction}",
        }

        # Mock client matching the genai.Client interface the code uses
        mock_client = Mock()
        mock_response = Mock()
        mock_response.text = "Test analysis"
        mock_response.candidates = None
        mock_client.models.generate_content.return_value = mock_response

        pr_details = {"title": "Test PR", "body": "Test body", "files_changed": ["test.py"], "diff": "test diff"}

        result = analyze_with_gemini(mock_client, pr_details)
        self.assertEqual(result, "Test analysis")
        mock_client.models.generate_content.assert_called_once()

    def test_parse_diff_for_suggestions_valid(self):
        """Test parsing diff suggestions."""
        diff_text = """--- a/test.py
+++ b/test.py
@@ -1,1 +1,1 @@
-old line
+new line"""
        result = parse_diff_for_suggestions(diff_text)
        self.assertIsNotNone(result)
        file_path, line, suggestion = result
        self.assertEqual(file_path, "test.py")
        self.assertEqual(line, 1)
        self.assertEqual(suggestion, "new line")

    def test_parse_diff_for_suggestions_invalid(self):
        """Test parsing invalid diff."""
        diff_text = "not a diff"
        result = parse_diff_for_suggestions(diff_text)
        self.assertIsNone(result)

    def test_find_diff_position(self):
        """Test finding position in diff hunk."""
        import textwrap

        diff = textwrap.dedent(
            """\
            diff --git a/test.py b/test.py
            @@ -1,3 +1,3 @@
             old line 1
            -old line 2
            +new line 2
             old line 3"""
        ).strip()
        position = find_diff_position(diff, "test.py", 2)
        self.assertEqual(position, 3)

    @patch("harperbot.harperbot.load_config")
    def test_load_config_with_authoring_defaults(self, mock_load_config):
        """Test loading config with authoring defaults."""
        with patch("os.path.exists", return_value=False):
            config = load_config()
            self.assertFalse(config["enable_authoring"])
            self.assertFalse(config["auto_commit_suggestions"])
            self.assertFalse(config["create_improvement_prs"])
            self.assertEqual(config["improvement_branch_pattern"], "harperbot-improvements-{timestamp}")

    def test_create_branch_success(self):
        """Test successful branch creation."""
        mock_repo = Mock()
        mock_base_ref = Mock()
        mock_base_ref.object.sha = "abc123"
        mock_new_ref = Mock()
        mock_repo.get_git_ref.side_effect = [Exception(), mock_base_ref, mock_new_ref]

        result = create_branch(mock_repo, "main", "feature-branch")
        mock_repo.create_git_ref.assert_called_once()
        self.assertEqual(result, mock_new_ref)

    def test_apply_suggestions_to_pr(self):
        """Test applying suggestions to PR."""
        mock_repo = Mock()
        mock_pr = Mock()
        mock_pr.number = 123
        mock_pr.head.ref = "feature-branch"
        mock_pr.head.sha = "def456"

        mock_ref = Mock()
        mock_repo.get_git_ref.return_value = mock_ref

        suggestions = [("test.py", "1", "new content")]

        # Mock file content
        mock_file = Mock()
        mock_file.decoded_content.decode.return_value = "old content"
        mock_repo.get_contents.return_value = mock_file

        apply_suggestions_to_pr(mock_repo, mock_pr, suggestions)

        # Should create commit with changes
        self.assertTrue(mock_repo.create_git_commit.called)

    def test_apply_suggestions_single_line(self):
        """Test applying a single-line suggestion and verify content transformation."""
        mock_repo = Mock()
        mock_pr = Mock()
        mock_pr.number = 123
        mock_pr.head.ref = "feature-branch"
        mock_pr.head.sha = "def456"

        mock_ref = Mock()
        mock_repo.get_git_ref.return_value = mock_ref

        suggestions = [("test.py", "1", "new line content")]

        # Mock file content with multiple lines
        mock_file = Mock()
        mock_file.decoded_content.decode.return_value = "line 1\nline 2\nline 3"
        mock_repo.get_contents.return_value = mock_file

        apply_suggestions_to_pr(mock_repo, mock_pr, suggestions)

        # Verify commit was created
        self.assertTrue(mock_repo.create_git_commit.called)

        # To verify content, check that create_git_blob was called with the transformed content
        # The transformed content should be "new line content\nline 2\nline 3"
        expected_content = "new line content\nline 2\nline 3"
        mock_repo.create_git_blob.assert_called_with(expected_content, "utf-8")

    def test_apply_suggestions_multi_line(self):
        """Test applying a multi-line suggestion."""
        mock_repo = Mock()
        mock_pr = Mock()
        mock_pr.number = 123
        mock_pr.head.ref = "feature-branch"
        mock_pr.head.sha = "def456"

        mock_ref = Mock()
        mock_repo.get_git_ref.return_value = mock_ref

        suggestions = [("test.py", "2", "new line 1\nnew line 2\nnew line 3")]

        mock_file = Mock()
        mock_file.decoded_content.decode.return_value = "line 1\nline 2\nline 3"
        mock_repo.get_contents.return_value = mock_file

        apply_suggestions_to_pr(mock_repo, mock_pr, suggestions)

        self.assertTrue(mock_repo.create_git_commit.called)
        # Expected: "line 1\nnew line 1\nnew line 2\nnew line 3\nline 3"
        expected_content = "line 1\nnew line 1\nnew line 2\nnew line 3\nline 3"
        mock_repo.create_git_blob.assert_called_with(expected_content, "utf-8")

    def test_apply_suggestions_out_of_bounds(self):
        """Test applying a suggestion with out-of-bounds line number."""
        mock_repo = Mock()
        mock_pr = Mock()
        mock_pr.number = 123
        mock_pr.head.ref = "feature-branch"
        mock_pr.head.sha = "def456"

        mock_ref = Mock()
        mock_repo.get_git_ref.return_value = mock_ref

        suggestions = [("test.py", "10", "new content")]  # Line 10, but file has only 3 lines

        mock_file = Mock()
        mock_file.decoded_content.decode.return_value = "line 1\nline 2\nline 3"
        mock_repo.get_contents.return_value = mock_file

        apply_suggestions_to_pr(mock_repo, mock_pr, suggestions)

        # Should not create commit since suggestion is skipped
        self.assertFalse(mock_repo.create_git_commit.called)


    def _comment(self, body):
        c = Mock()
        c.body = body
        return c

    def test_upsert_creates_when_none_exist(self):
        pr = Mock()
        pr.number = 1
        pr.get_issue_comments.return_value = [self._comment("human words")]
        upsert_analysis_comment(pr, "new body")
        pr.create_issue_comment.assert_called_once_with("new body")

    def test_upsert_edits_single_existing(self):
        old = self._comment("analysis " + ANALYSIS_MARKER)
        pr = Mock()
        pr.number = 1
        pr.get_issue_comments.return_value = [old]
        upsert_analysis_comment(pr, "new body")
        pr.create_issue_comment.assert_not_called()
        old.edit.assert_called_once_with("new body")

    def test_upsert_adopts_legacy_comment(self):
        legacy = self._comment("<summary>HarperBot Analysis</summary> "
                               "harperbot/actions/workflows stuff")
        pr = Mock()
        pr.number = 1
        pr.get_issue_comments.return_value = [legacy]
        self.assertEqual(find_existing_analysis_comments(pr), [legacy])
        upsert_analysis_comment(pr, "new body")
        legacy.edit.assert_called_once_with("new body")

    def test_upsert_deletes_extras(self):
        first = self._comment("one " + ANALYSIS_MARKER)
        second = self._comment("two " + ANALYSIS_MARKER)
        pr = Mock()
        pr.number = 1
        pr.get_issue_comments.return_value = [first, second]
        upsert_analysis_comment(pr, "new body")
        first.edit.assert_called_once_with("new body")
        second.delete.assert_called_once_with()


class TestErrorHandling(unittest.TestCase):
    """Classifier, missing-key check, and request-shape construction."""

    def _api_error(self, code):
        from google.genai import errors
        return errors.APIError(code, "resp", {"error": {"message": "m"}})

    def test_auth_codes_map_to_auth_message(self):
        for code in (401, 403):
            msg = classify_provider_error(self._api_error(code), " (ctx)")
            self.assertIn("Invalid API key or authentication failed", msg)
            self.assertIn("(ctx)", msg)

    def test_quota_code_maps_to_quota_message(self):
        msg = classify_provider_error(self._api_error(429), "")
        self.assertIn("quota exceeded", msg)

    def test_server_codes_map_to_unavailable(self):
        for code in (500, 503):
            msg = classify_provider_error(self._api_error(code), "")
            self.assertIn("API unavailable", msg)

    def test_unknown_model_codes_map_to_unavailable_model(self):
        for code in (400, 404):
            msg = classify_provider_error(self._api_error(code), "")
            self.assertIn("Requested model not available", msg)

    def test_transport_errors_are_transient_not_internal(self):
        from cerebras.cloud.sdk._exceptions import (
            APIConnectionError,
            APITimeoutError,
        )

        class FakeTimeout(TimeoutError):
            pass

        request = Mock()
        for err in (APITimeoutError(request=request),
                    APIConnectionError(request=request),
                    FakeTimeout("t"), ConnectionResetError("c")):
            msg = classify_provider_error(err, "")
            self.assertIn("temporarily unreachable", msg)

    def test_httpx_protocol_error_is_transient(self):
        """A mid-response disconnect is transport, not a bot bug."""
        import httpx
        from unittest.mock import Mock
        err = httpx.RemoteProtocolError("gone", request=Mock())
        msg = classify_provider_error(err, "")
        self.assertIn("temporarily unreachable", msg)

    def test_unknown_error_raises_internal(self):
        with self.assertRaises(BotInternalError) as cm:
            classify_provider_error(ValueError("weird"), "")
        self.assertEqual(cm.exception.step, "analysis")

    def test_missing_key_names_variable_not_value(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CEREBRAS_API_KEY", None)
            with patch("harperbot.harperbot.load_config",
                       return_value={"provider": "cerebras"}):
                msg = analyze_with_gemini(
                    Mock(), {"diff": "", "files_changed": [], "title": "t"})
                self.assertIn("CEREBRAS_API_KEY is not set", msg)
                self.assertNotIn("sk-", msg)

    def test_tools_request_shape_constructs(self):
        from google.genai import types
        tools = [types.Tool(function_declarations=[
            types.FunctionDeclaration(name="f", description="d")])]
        cfg = types.GenerateContentConfig(
            thinking_config=types.ThinkingConfig(thinking_level="medium"),
            max_output_tokens=100,
            tools=tools,
        )
        self.assertEqual(cfg.thinking_config.thinking_level, "MEDIUM")

    def test_run_url_present_and_absent(self):
        with patch.dict(os.environ, {"GITHUB_SERVER_URL": "https://g.io",
                                     "GITHUB_REPOSITORY": "o/r",
                                     "GITHUB_RUN_ID": "1"}):
            self.assertEqual(workflow_run_url(),
                             "https://g.io/o/r/actions/runs/1")
        with patch.dict(os.environ, {}, clear=False):
            for k in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID"):
                os.environ.pop(k, None)
            self.assertIsNone(workflow_run_url())

    def test_tools_absent_when_flag_off(self):
        """With custom_functions off, the request carries no tools."""
        from unittest.mock import Mock
        from unittest.mock import patch as _patch
        with _patch("harperbot.harperbot.load_config", return_value={
                "provider": "gemini", "model": "m", "focus": "all",
                "max_diff_length": 4000, "max_output_tokens": 100,
                "prompt": "{num_files} {files_list} {diff_content} {focus_instruction}",
                "custom_functions": False}):
            with _patch.dict("os.environ", {"GEMINI_API_KEY": "k"}):
                client = Mock()
                resp = Mock()
                resp.text = "done"
                resp.candidates = None
                client.models.generate_content.return_value = resp
                from harperbot.harperbot import analyze_with_gemini
                out = analyze_with_gemini(
                    client, {"diff": "d", "files_changed": ["f"], "title": "t"})
                self.assertEqual(out, "done")
                cfg = client.models.generate_content.call_args[1]["config"]
                self.assertIsNone(cfg.tools)

    def test_function_call_only_response_reports_parts(self):
        """A textless reply names its finish reason and part kinds."""
        from types import SimpleNamespace
        from unittest.mock import Mock
        from unittest.mock import patch as _patch
        part = SimpleNamespace(text=None,
                               function_call=SimpleNamespace(name="check_python_syntax"))
        cand = SimpleNamespace(content=SimpleNamespace(parts=[part]),
                               finish_reason="STOP")
        mock_client = Mock()
        mock_resp = Mock()
        mock_resp.text = None
        mock_resp.candidates = [cand]
        mock_client.models.generate_content.return_value = mock_resp
        with _patch("harperbot.harperbot.load_config", return_value={
                "provider": "gemini", "model": "m", "focus": "all",
                "max_diff_length": 4000, "max_output_tokens": 100,
                "prompt": "{num_files} {files_list} {diff_content} {focus_instruction}",
                "custom_functions": False}):
            with _patch.dict("os.environ", {"GEMINI_API_KEY": "k"}):
                from harperbot.harperbot import analyze_with_gemini, BotInternalError
                try:
                    analyze_with_gemini(
                        mock_client, {"diff": "d", "files_changed": ["f"],
                                      "title": "t"})
                    self.fail("expected BotInternalError")
                except BotInternalError as e:
                    self.assertIn("function_call", e.public_message)
                    self.assertIn("STOP", e.public_message)

    def test_null_function_call_is_other_not_a_call(self):
        """A part with function_call=None must not crash the logger."""
        from types import SimpleNamespace
        from unittest.mock import Mock
        from unittest.mock import patch as _patch
        part = SimpleNamespace(text=None, function_call=None)
        cand = SimpleNamespace(content=SimpleNamespace(parts=[part]),
                               finish_reason="STOP")
        mock_client = Mock()
        mock_resp = Mock()
        mock_resp.text = None
        mock_resp.candidates = [cand]
        mock_client.models.generate_content.return_value = mock_resp
        with _patch("harperbot.harperbot.load_config", return_value={
                "provider": "gemini", "model": "m", "focus": "all",
                "max_diff_length": 4000, "max_output_tokens": 100,
                "prompt": "{num_files} {files_list} {diff_content} {focus_instruction}",
                "custom_functions": False}):
            with _patch.dict("os.environ", {"GEMINI_API_KEY": "k"}):
                from harperbot.harperbot import analyze_with_gemini, BotInternalError
                try:
                    analyze_with_gemini(
                        mock_client, {"diff": "d", "files_changed": ["f"],
                                      "title": "t"})
                    self.fail("expected BotInternalError")
                except BotInternalError as e:
                    self.assertIn("other", e.public_message)
                    self.assertNotIn("function_call", e.public_message)

    def test_blocked_response_names_block_reason(self):
        """A blocked reply names the block reason, not a generic message."""
        from types import SimpleNamespace
        from unittest.mock import Mock
        from unittest.mock import patch as _patch
        cand = SimpleNamespace(content=SimpleNamespace(parts=[]),
                               finish_reason="OTHER")
        mock_client = Mock()
        mock_resp = Mock()
        mock_resp.text = None
        mock_resp.candidates = [cand]
        mock_resp.prompt_feedback = SimpleNamespace(block_reason="SAFETY")
        mock_client.models.generate_content.return_value = mock_resp
        with _patch("harperbot.harperbot.load_config", return_value={
                "provider": "gemini", "model": "m", "focus": "all",
                "max_diff_length": 4000, "max_output_tokens": 100,
                "prompt": "{num_files} {files_list} {diff_content} {focus_instruction}",
                "custom_functions": False}):
            with _patch.dict("os.environ", {"GEMINI_API_KEY": "k"}):
                from harperbot.harperbot import analyze_with_gemini, BotInternalError
                try:
                    analyze_with_gemini(
                        mock_client, {"diff": "d", "files_changed": ["f"],
                                      "title": "t"})
                    self.fail("expected BotInternalError")
                except BotInternalError as e:
                    self.assertIn("SAFETY", e.public_message)

    def test_usage_logging_gemini_shape(self):
        """Gemini usage object logs all counts including thinking tokens."""
        from types import SimpleNamespace
        from harperbot.harperbot import log_api_usage
        usage = SimpleNamespace(prompt_token_count=100, candidates_token_count=20,
                                thoughts_token_count=3000, total_token_count=3120)
        cand = SimpleNamespace(finish_reason="MAX_TOKENS")
        resp = SimpleNamespace(usage_metadata=usage, candidates=[cand])
        with self.assertLogs(level="INFO") as cm:
            log_api_usage(resp, "m", "gemini")
        line = " ".join(cm.output)
        self.assertIn("thinking=3000", line)
        self.assertIn("finish=MAX_TOKENS", line)
        self.assertIn("model=m provider=gemini", line)

    def test_usage_logging_openai_shape(self):
        """Cerebras/OpenAI usage object: prompt/completion/total tokens."""
        from types import SimpleNamespace
        from harperbot.harperbot import log_api_usage
        usage = SimpleNamespace(prompt_tokens=50, completion_tokens=10,
                                total_tokens=60)
        choice = SimpleNamespace(finish_reason="stop")
        resp = SimpleNamespace(usage=usage, choices=[choice])
        with self.assertLogs(level="INFO") as cm:
            log_api_usage(resp, "m", "cerebras")
        line = " ".join(cm.output)
        self.assertIn("prompt=50", line)
        self.assertIn("finish=stop", line)

    def test_usage_logging_never_fails(self):
        """Absent or hostile shapes degrade to Nones, never raise."""
        from unittest.mock import Mock
        from harperbot.harperbot import log_api_usage
        log_api_usage(None, "m", "gemini")
        log_api_usage(Mock(), "m", "gemini")
        log_api_usage(object(), "m", "gemini")
class TestOpenRouterProvider(unittest.TestCase):
    """OpenRouter branch: no network, requests.post mocked."""

    def _config(self):
        return {
            "provider": "openrouter", "focus": "all",
            "max_diff_length": 4000, "max_output_tokens": 100,
            "prompt": "{num_files} {files_list} {diff_content} {focus_instruction}",
        }

    def _details(self):
        return {"diff": "d", "files_changed": ["f"], "title": "t"}

    def test_missing_key_names_variable(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENROUTER_API_KEY", None)
            with patch("harperbot.harperbot.load_config", return_value=self._config()):
                from harperbot.harperbot import analyze_with_gemini
                msg = analyze_with_gemini(Mock(), self._details())
                self.assertIn("OPENROUTER_API_KEY is not set", msg)

    def test_success_returns_text_and_logs_resolved_model(self):
        import requests
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "k"}):
            with patch("harperbot.harperbot.load_config", return_value=self._config()):
                with patch("harperbot.harperbot.requests.post") as mp:
                    resp = Mock()
                    resp.json.return_value = {
                        "model": "openai/gpt-5.2",
                        "choices": [{"message": {"content": "looks good"}}]}
                    mp.return_value = resp
                    from harperbot.harperbot import analyze_with_gemini
                    out = analyze_with_gemini(Mock(), self._details())
                    self.assertEqual(out, "looks good")
                    body = mp.call_args[1]["json"]
                    self.assertEqual(body["model"], "openrouter/auto")
                    self.assertEqual(body["messages"][0]["role"], "user")

    def test_auth_failure_maps_to_auth_message(self):
        import requests
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "k"}):
            with patch("harperbot.harperbot.load_config", return_value=self._config()):
                with patch("harperbot.harperbot.requests.post") as mp:
                    err_resp = Mock()
                    err_resp.status_code = 401
                    mp.side_effect = requests.HTTPError(response=err_resp)
                    from harperbot.harperbot import analyze_with_gemini
                    msg = analyze_with_gemini(Mock(), self._details())
                    self.assertIn("Invalid API key or authentication failed", msg)
                    self.assertIn("openrouter", msg)

class TestOpenRouterFallback(unittest.TestCase):
    """Gemini primary with OpenRouter fallback on traffic errors."""

    def _503(self):
        from google.genai import errors
        return errors.APIError(503, "resp", {"error": {"message": "x"}})

    def _401(self):
        from google.genai import errors
        return errors.APIError(401, "resp", {"error": {"message": "x"}})

    def _gemini_config(self):
        return {
            "provider": "gemini", "model": "gemini-3.8-flash", "focus": "all",
            "max_diff_length": 4000, "max_output_tokens": 100,
            "prompt": "{num_files} {files_list} {diff_content} {focus_instruction}",
            "openrouter_model": "openrouter/auto",
        }

    def _details(self):
        return {"diff": "d", "files_changed": ["f"], "title": "t"}

    def test_fallback_returns_openrouter_text(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "k",
                                     "OPENROUTER_API_KEY": "k"}):
            with patch("harperbot.harperbot.load_config", return_value=self._gemini_config()):
                with patch("harperbot.harperbot.requests.post") as mp:
                    resp = Mock()
                    resp.json.return_value = {
                        "model": "deepseek/deepseek-v4.1-flash",
                        "choices": [{"message": {"content": "fallback review"}}]}
                    mp.return_value = resp
                    client = Mock()
                    client.models.generate_content.side_effect = self._503()
                    from harperbot.harperbot import analyze_with_gemini
                    out = analyze_with_gemini(client, self._details())
                    self.assertEqual(out, "fallback review")

    def test_auth_error_does_not_fallback(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "k",
                                     "OPENROUTER_API_KEY": "k"}):
            with patch("harperbot.harperbot.load_config", return_value=self._gemini_config()):
                with patch("harperbot.harperbot.requests.post") as mp:
                    client = Mock()
                    client.models.generate_content.side_effect = self._401()
                    from harperbot.harperbot import analyze_with_gemini
                    msg = analyze_with_gemini(client, self._details())
                    self.assertIn("Invalid API key", msg)
                    mp.assert_not_called()

    def test_no_fallback_without_key(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "k"}, clear=False):
            os.environ.pop("OPENROUTER_API_KEY", None)
            with patch("harperbot.harperbot.load_config", return_value=self._gemini_config()):
                with patch("harperbot.harperbot.requests.post") as mp:
                    client = Mock()
                    client.models.generate_content.side_effect = self._503()
                    from harperbot.harperbot import analyze_with_gemini
                    msg = analyze_with_gemini(client, self._details())
                    self.assertIn("API unavailable", msg)
                    mp.assert_not_called()

    def test_double_failure_names_both(self):
        import requests
        with patch.dict(os.environ, {"GEMINI_API_KEY": "k",
                                     "OPENROUTER_API_KEY": "k"}):
            with patch("harperbot.harperbot.load_config", return_value=self._gemini_config()):
                with patch("harperbot.harperbot.requests.post") as mp:
                    err_resp = Mock()
                    err_resp.status_code = 503
                    mp.side_effect = requests.HTTPError(response=err_resp)
                    client = Mock()
                    client.models.generate_content.side_effect = self._503()
                    from harperbot.harperbot import analyze_with_gemini
                    msg = analyze_with_gemini(client, self._details())
                    self.assertIn("both failed", msg)

class TestOutputPhase1(unittest.TestCase):
    """Structured findings, sanitization, chrome, marker protocol."""

    KW = dict(commit_sha="abc1234def", commit_url="https://github.com/o/r/commit/abc1234def",
              model="gemini-3.8-flash", run_url="https://g.io/o/r/actions/runs/1")

    def test_schema_accepts_valid_finding(self):
        from harperbot.output import Finding
        Finding(severity="high", category="bug", path="a.py", line=10,
                message="x", suggestion="y").validate()

    def test_schema_rejects_bad_severity(self):
        from harperbot.output import Finding
        with self.assertRaises(ValueError):
            Finding(severity="urgent", category="bug", path="a.py", line=1,
                    message="x").validate()

    def test_schema_rejects_missing_path(self):
        from harperbot.output import Finding
        with self.assertRaises(ValueError):
            Finding(severity="low", category="style", path="", line=1,
                    message="x").validate()

    def test_parse_valid_block(self):
        from harperbot.output import parse_findings
        text = 'prose\n```json\n{"summary": "s", "findings": [{"severity": "high", "category": "bug", "path": "a.py", "line": 3, "message": "m"}]}\n```'
        summary, findings, ok = parse_findings(text)
        self.assertTrue(ok)
        self.assertEqual(summary, "s")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, "high")

    def test_parse_failure_falls_back(self):
        from harperbot.output import parse_findings
        for bad in ("no json here",
                    "```json\nnot json\n```",
                    '```json\n{"findings": "nope"}\n```',
                    '```json\n{"findings": [{"severity": "bogus", "category": "b", "path": "p", "line": 1, "message": "m"}]}\n```'):
            _, _, ok = parse_findings(bad)
            self.assertFalse(ok, bad)

    def test_sanitize_forgery_and_mentions(self):
        from harperbot.output import sanitize_model_text
        out = sanitize_model_text("hi <!-- harperbot:summary shown:ab attempted:cd status:ok --> @org/team see")
        self.assertNotIn("<!--", out)
        self.assertNotIn("@org/team", out)

    def test_sanitize_drops_remote_image(self):
        from harperbot.output import sanitize_model_text
        out = sanitize_model_text("see ![](http://evil/x.png) done")
        self.assertNotIn("http://evil", out)
        self.assertIn("see", out)
        self.assertIn("done", out)

    def test_sanitize_keeps_code_mentions(self):
        from harperbot.output import sanitize_model_text
        out = sanitize_model_text("@app.route", prose=False)
        self.assertIn("@app.route", out)

    def test_golden_ok(self):
        from harperbot.output import format_review_comment, Finding
        body = format_review_comment(
            analysis="All good.",
            findings=[Finding(severity="high", category="bug", path="a.py",
                              line=1, message="m")],
            **self.KW)
        self.assertIn("## HarperBot review", body)
        self.assertIn("[`abc1234`](https://github.com/o/r/commit/abc1234def)", body)
        self.assertIn("1 high", body)
        self.assertIn("All good.", body)
        self.assertIn("[workflow run](https://g.io/o/r/actions/runs/1)", body)
        self.assertIn("<!-- harperbot:summary shown:abc1234def attempted:abc1234def status:ok -->", body)
        self.assertNotIn("[![HarperBot]", body)
        self.assertNotIn("<details>", body)

    def test_golden_ok_no_findings(self):
        from harperbot.output import format_review_comment
        body = format_review_comment(analysis="Clean.", findings=[], **self.KW)
        self.assertIn("no findings", body)

    def test_golden_fallback(self):
        from harperbot.output import format_fallback_comment
        body = format_fallback_comment(analysis="raw text", **self.KW)
        self.assertIn("Structured findings unavailable", body)
        self.assertIn("<details>", body)
        self.assertIn("raw text", body)

    def test_golden_failed_none(self):
        from harperbot.output import format_failure_notice
        body = format_failure_notice(attempted_sha="abc1234def",
                                     run_url="https://g.io/o/r/actions/runs/1")
        self.assertNotIn("HarperBot review", body)
        self.assertNotIn("<details>", body)
        self.assertIn("abc1234", body)
        self.assertIn("actions/runs/1", body)

    def test_banner_updates_only_banner_and_marker(self):
        from harperbot.output import apply_failure_banner
        prior = ("## HarperBot review\n\nReviewed [`abc`](u) · m · 1 high\n\n"
                 "Body text here.\n\n<sub>x</sub>\n\n"
                 "<!-- harperbot:summary shown:abc1234 attempted:abc1234 status:ok -->")
        out = apply_failure_banner(body=prior, attempted_sha="def4567",
                                   run_url="https://g.io/r")
        self.assertIn("Body text here.", out)
        self.assertIn("Last run failed on `def4567`", out)
        self.assertIn("status:failed", out)
        self.assertIn("shown:abc1234", out)
        self.assertNotIn("status:ok", out)

    def test_marker_round_trip_last_wins(self):
        from harperbot.output import parse_marker, render_marker
        forged = render_marker("aaaabbbb", "aaaabbbb", "ok")
        real = render_marker("bbbbcccc", "ccccdddd", "failed")
        parsed = parse_marker(forged + "\n" + real)
        self.assertEqual(parsed["shown"], "bbbbcccc")
        self.assertEqual(parsed["attempted"], "ccccdddd")
        self.assertEqual(parsed["status"], "failed")
        self.assertIsNone(parse_marker("no marker here"))

    def test_counts_line(self):
        from harperbot.output import counts_line, Finding
        mk = lambda s: Finding(severity=s, category="c", path="p", line=1, message="m")
        self.assertEqual(counts_line([]), "no findings")
        self.assertEqual(counts_line([mk("high"), mk("high"), mk("low")]),
                         "2 high · 1 low")

class TestCheckRun(unittest.TestCase):
    """Check summary, annotations, chunking, conclusions."""

    def test_conclusion_never_failure(self):
        from harperbot.output import check_conclusion, Finding
        mk = lambda s: Finding(severity=s, category="c", path="p", line=1, message="m")
        self.assertEqual(check_conclusion([], False), "success")
        self.assertEqual(check_conclusion([mk("low")], False), "neutral")
        self.assertEqual(check_conclusion([], True), "neutral")
        self.assertNotIn("failure", {check_conclusion([], False),
                                     check_conclusion([mk("high")], False),
                                     check_conclusion([], True)})

    def test_in_diff_excluded_from_annotations(self):
        from harperbot.output import (parse_diff_added_lines,
                                      split_findings_by_diff, Finding)
        diff = ("+++ b/a.py\n@@ -1,3 +1,4 @@\n x\n+y\n z\n"
                "+++ b/c.py\n@@ -10,2 +10,3 @@\n p\n+q\n r\n")
        added = parse_diff_added_lines(diff)
        in_f = Finding(severity="high", category="bug", path="a.py", line=2,
                       message="m")
        out_f = Finding(severity="low", category="style", path="a.py", line=9,
                        message="m")
        other_f = Finding(severity="medium", category="perf", path="z.py",
                          line=1, message="m")
        in_diff, out = split_findings_by_diff([in_f, out_f, other_f], added)
        self.assertEqual(in_diff, [in_f])
        self.assertEqual(out, [out_f, other_f])

    def test_annotation_levels_and_shape(self):
        from harperbot.output import finding_to_annotation, Finding
        hi = finding_to_annotation(Finding(severity="critical", category="c",
                                           path="p", line=5, message="m"))
        self.assertEqual(hi["annotation_level"], "warning")
        self.assertEqual((hi["start_line"], hi["end_line"]), (5, 5))
        lo = finding_to_annotation(Finding(severity="info", category="c",
                                           path="p", line=None, message="m"))
        self.assertEqual(lo["annotation_level"], "notice")
        self.assertEqual(lo["start_line"], 1)
        self.assertNotIn("error", {hi["annotation_level"],
                                   lo["annotation_level"]})

    def test_chunking_at_fifty(self):
        from harperbot.output import chunk_annotations
        anns = [{"n": i} for i in range(51)]
        chunks = chunk_annotations(anns)
        self.assertEqual([len(c) for c in chunks], [50, 1])
        self.assertEqual(chunk_annotations([]), [])

    def test_check_summary_shape(self):
        from harperbot.output import build_check_summary, Finding
        mk = lambda s: Finding(severity=s, category="c", path="p", line=1, message="m")
        body = build_check_summary(commit_sha="abc1234", model="m",
                                   findings=[mk("high")],
                                   analysis="analysis text here")
        self.assertIn("abc1234", body)
        self.assertIn("1 high", body)
        self.assertIn("<details>", body)
        self.assertIn("analysis text here", body)

    def test_diff_parser_ranges(self):
        from harperbot.output import parse_diff_added_lines
        diff = "+++ b/a.py\n@@ -1,3 +10,4 @@\n x\n+y\n+z\n w\n"
        self.assertEqual(parse_diff_added_lines(diff), {"a.py": [(11, 12)]})

class TestReviewPostingFlag(unittest.TestCase):
    """Inline review posting stays off unless explicitly enabled."""

    def _analysis_with_suggestion(self):
        return ("text\n```diff\n--- a/f.py\n+++ b/f.py\n"
                "@@ -1 +1 @@\n-old\n+new\n```")

    def _pr(self):
        from unittest.mock import Mock
        pr = Mock()
        pr.number = 1
        pr.get_issue_comments.return_value = []
        return pr

    def test_flag_off_skips_both_paths(self):
        from unittest.mock import Mock, patch
        from harperbot.harperbot import post_comment_webhook
        with patch("harperbot.harperbot.load_config",
                   return_value={"focus": "all", "model": "m",
                                 "post_inline_reviews": False,
                                 "post_suggestions_in_yaml": True}):
            with patch("harperbot.harperbot.save_inline_suggestions") as sv, \
                 patch("harperbot.harperbot.post_inline_suggestions") as ps:
                with patch("harperbot.harperbot.Github") as gh:
                    repo = gh.return_value.get_repo.return_value
                    repo.get_commit.return_value.get_check_runs.return_value = []
                    repo.get_pull.return_value.get_issue_comments.return_value = []
                    post_comment_webhook(
                    "tok", "o/r",
                        {"number": 1, "head_sha": "abc1234", "diff": "",
                         "model_name": "m"},
                        self._analysis_with_suggestion())
                    sv.assert_not_called()
                    ps.assert_not_called()

    def test_flag_on_preserves_existing_routing(self):
        from unittest.mock import Mock, patch
        from harperbot.harperbot import post_comment_webhook
        with patch("harperbot.harperbot.load_config",
                   return_value={"focus": "all", "model": "m",
                                 "post_inline_reviews": True,
                                 "post_suggestions_in_yaml": True}):
            with patch("harperbot.harperbot.save_inline_suggestions") as sv:
                with patch("harperbot.harperbot.Github") as gh:
                    repo = gh.return_value.get_repo.return_value
                    pr = repo.get_pull.return_value
                    pr.get_issue_comments.return_value = []
                    repo.get_commit.return_value.get_check_runs.return_value = []
                    post_comment_webhook(
                        "tok", "o/r",
                        {"number": 1, "head_sha": "abc1234", "diff": "",
                         "model_name": "m"},
                        self._analysis_with_suggestion())
                    self.assertTrue(sv.called)
class TestCheckRunActions(unittest.TestCase):
    """Re-run action present on create and edit, with the right identifier."""

    def _repo(self):
        from unittest.mock import Mock
        repo = Mock()
        commit = repo.get_commit.return_value
        commit.get_check_runs.return_value = []
        return repo

    def test_create_carries_action(self):
        from harperbot.harperbot import upsert_check_run
        repo = self._repo()
        upsert_check_run(repo, "abc1234", summary="s", conclusion="success",
                         annotations=[], run_url=None)
        _, kwargs = repo.create_check_run.call_args
        actions = kwargs["actions"]
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["identifier"], "rerun-analysis")
        self.assertTrue(actions[0]["label"])

    def test_edit_preserves_action(self):
        from unittest.mock import Mock
        from harperbot.harperbot import upsert_check_run
        repo = Mock()
        existing = Mock()
        repo.get_commit.return_value.get_check_runs.return_value = [existing]
        upsert_check_run(repo, "abc1234", summary="s", conclusion="neutral",
                         annotations=[], run_url=None)
        _, kwargs = existing.edit.call_args
        self.assertEqual(kwargs["actions"][0]["identifier"], "rerun-analysis")

    def test_action_fields_within_api_limits(self):
        # The check-runs API caps action descriptions at 40 chars;
        # exceeding it fails the whole create call with a 422.
        from harperbot.output import CHECK_RUN_ACTIONS
        for action in CHECK_RUN_ACTIONS:
            self.assertLessEqual(len(action["description"]), 40)

    def test_at_most_three_actions(self):
        from harperbot.output import CHECK_RUN_ACTIONS
        self.assertLessEqual(len(CHECK_RUN_ACTIONS), 3)
        for action in CHECK_RUN_ACTIONS:
            self.assertIn("identifier", action)

class TestProviders(unittest.TestCase):
    """Auto-resolution, listing, validation, plugins. No network."""

    def _cfg(self, **over):
        cfg = {"provider": "auto",
               "provider_order": ["gemini", "openrouter", "cerebras"]}
        cfg.update(over)
        return cfg

    def test_auto_picks_first_keyed_provider(self):
        from harperbot import providers
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "k"}, clear=False):
            for v in ("GEMINI_API_KEY", "CEREBRAS_API_KEY"):
                os.environ.pop(v, None)
            name, key, err = providers.resolve_provider(self._cfg())
            self.assertEqual((name, key, err),
                             ("openrouter", "OPENROUTER_API_KEY", None))

    def test_auto_prefers_order(self):
        from harperbot import providers
        with patch.dict(os.environ, {"GEMINI_API_KEY": "g",
                                     "OPENROUTER_API_KEY": "o"}):
            name, _, _ = providers.resolve_provider(self._cfg())
            self.assertEqual(name, "gemini")

    def test_auto_no_keys_specific_error(self):
        from harperbot import providers
        with patch.dict(os.environ, {}, clear=False):
            for v in ("GEMINI_API_KEY", "OPENROUTER_API_KEY", "CEREBRAS_API_KEY"):
                os.environ.pop(v, None)
            name, _, err = providers.resolve_provider(self._cfg())
            self.assertIsNone(name)
            self.assertIn("No provider API key", err)

    def test_unknown_provider_errors(self):
        from harperbot import providers
        name, _, err = providers.resolve_provider({"provider": "wat"})
        self.assertIsNone(name)
        self.assertIn("Unknown provider", err)

    def test_fixture_parses_and_filters_tools(self):
        import json
        fixture = json.load(open("tests/fixtures/openrouter_models.json"))
        ids = [m["id"] for m in fixture["data"]]
        self.assertIn("openrouter/auto", ids)
        tools_ok = [m["id"] for m in fixture["data"]
                    if "tools" in (m.get("supported_parameters") or [])]
        self.assertTrue(tools_ok)
        self.assertEqual(len(tools_ok), len(ids) - 1)

    def test_plugins_only_for_auto(self):
        from harperbot import providers
        cfg = {"openrouter": {"allowed_models": ["a/*"], "cost_tier": "high"}}
        plugins = providers.openrouter_plugins(cfg, "openrouter/auto")
        self.assertEqual(plugins[0]["id"], "auto-router")
        self.assertEqual(plugins[0]["allowed_models"], ["a/*"])
        self.assertEqual(plugins[0]["cost_tier"], "high")
        self.assertIsNone(providers.openrouter_plugins(cfg, "openai/gpt-5.2"))

    def test_model_validation_warns_not_fails(self):
        from harperbot import providers
        with patch("harperbot.providers.list_models",
                   side_effect=RuntimeError("down")):
            self.assertTrue(providers.validate_model("openrouter", "x", None))

    def test_models_command_lists_and_exits_zero(self):
        import io
        from contextlib import redirect_stdout
        from unittest.mock import Mock
        from harperbot.harperbot import cmd_models
        args = Mock(provider="openrouter", command="models")
        with patch("harperbot.providers.list_models",
                   return_value=["openrouter/auto", "x/y"]) as ml:
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = cmd_models(args)
        self.assertEqual(code, 0)
        self.assertIn("openrouter/auto", buf.getvalue())
        ml.assert_called_once()

    def test_auto_end_to_end_selects_gemini(self):
        from unittest.mock import Mock, patch
        with patch.dict(os.environ, {"GEMINI_API_KEY": "k"}, clear=False):
            for v in ("OPENROUTER_API_KEY", "CEREBRAS_API_KEY"):
                os.environ.pop(v, None)
            with patch("harperbot.harperbot.load_config", return_value={
                    "provider": "auto",
                    "provider_order": ["gemini", "openrouter", "cerebras"],
                    "focus": "all", "max_diff_length": 4000,
                    "max_output_tokens": 100,
                    "prompt": "{num_files} {files_list} {diff_content} {focus_instruction}"}):
                client = Mock()
                resp = Mock()
                resp.text = "fine"
                resp.candidates = None
                client.models.generate_content.return_value = resp
                from harperbot.harperbot import analyze_with_gemini
                out = analyze_with_gemini(
                    client, {"diff": "d", "files_changed": ["f"], "title": "t"})
                self.assertEqual(out, "fine")

    def test_footer_shows_provider(self):
        from harperbot.harperbot import build_review_comment
        body, ok = build_review_comment(
            "text", {"head_sha": "abc1234", "model_name": "m",
                     "provider_name": "openrouter"},
            "o/r", {"model": "m"})
        self.assertIn("m via openrouter", body)

class TestDriftCheck(unittest.TestCase):
    """Drift detection: present, absent, unreachable, CLI codes."""

    def _cfg(self):
        return {"provider": "gemini", "provider_order": ["gemini"],
                "model": "gemini-3.8-flash"}

    def test_present_model_ok(self):
        from harperbot import providers
        with patch("harperbot.providers.list_models",
                   return_value=["gemini-3.8-flash", "other"]):
            [r] = providers.check_model_drift(
                self._cfg(), ["gemini"], environ={})
            self.assertTrue(r["ok"])
            self.assertEqual(r["model"], "gemini-3.8-flash")

    def test_absent_model_drift(self):
        from harperbot import providers
        with patch("harperbot.providers.list_models",
                   return_value=["other"]):
            [r] = providers.check_model_drift(
                self._cfg(), ["gemini"], environ={})
            self.assertFalse(r["ok"])
            self.assertIn("absent", r["detail"])

    def test_unreachable_listing_retries_then_reports(self):
        from harperbot import providers
        calls = []
        def flaky(*a, **k):
            calls.append(1)
            if len(calls) < 3:
                raise RuntimeError("boom")
            return ["gemini-3.8-flash"]
        with patch("harperbot.providers.list_models", side_effect=flaky):
            with patch("harperbot.providers.time.sleep") as sl:
                [r] = providers.check_model_drift(
                    self._cfg(), ["gemini"], environ={})
                self.assertTrue(r["ok"])
                self.assertEqual(len(calls), 3)
                self.assertEqual(sl.call_count, 2)

    def test_persistent_failure_reports_unreachable(self):
        from harperbot import providers
        with patch("harperbot.providers.list_models",
                   side_effect=RuntimeError("down")):
            with patch("harperbot.providers.time.sleep"):
                [r] = providers.check_model_drift(
                    self._cfg(), ["gemini"], environ={})
                self.assertFalse(r["ok"])
                self.assertIn("unreachable", r["detail"])

    def test_cli_exit_codes(self):
        import io
        from contextlib import redirect_stdout
        from unittest.mock import Mock
        from harperbot.harperbot import cmd_drift_check
        with patch("harperbot.providers.check_model_drift",
                   return_value=[{"provider": "g", "model": "m", "ok": True,
                                  "detail": "present"}]):
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(cmd_drift_check(Mock()), 0)
            self.assertIn("OK g m", buf.getvalue())
        with patch("harperbot.providers.check_model_drift",
                   return_value=[{"provider": "g", "model": "m", "ok": False,
                                  "detail": "absent"}]):
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(cmd_drift_check(Mock()), 1)
            self.assertIn("DRIFT g m", buf.getvalue())

if __name__ == "__main__":
    unittest.main()
