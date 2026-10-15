# SPDX-License-Identifier: MIT
# Copyright (c) 2026 coccinella-labs

#!/usr/bin/env python3
"""
Unit tests for HarperBot core functionality.
Run with: python -m pytest test/test_harperbot.py
"""

import os
import sys
import unittest
from unittest.mock import Mock, patch

# Add the harperbot directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "harperbot"))

from harperbot import (  # noqa: E402
    ANALYSIS_MARKER,
    BotInternalError,
    analyze_with_gemini,
    apply_suggestions_to_pr,
    create_branch,
    find_diff_position,
    load_config,
    find_existing_analysis_comments,
    format_comment,
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

    @patch("harperbot.load_config")
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

    @patch("harperbot.load_config")
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


    def test_format_comment_carries_marker(self):
        """The marker is what later runs match on."""
        self.assertIn(ANALYSIS_MARKER, format_comment("hello"))

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

    def test_unknown_error_raises_internal(self):
        with self.assertRaises(BotInternalError) as cm:
            classify_provider_error(ValueError("weird"), "")
        self.assertEqual(cm.exception.step, "analysis")

    def test_missing_key_names_variable_not_value(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CEREBRAS_API_KEY", None)
            with patch("harperbot.load_config",
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
        with _patch("harperbot.load_config", return_value={
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
                from harperbot import analyze_with_gemini
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
        with _patch("harperbot.load_config", return_value={
                "provider": "gemini", "model": "m", "focus": "all",
                "max_diff_length": 4000, "max_output_tokens": 100,
                "prompt": "{num_files} {files_list} {diff_content} {focus_instruction}",
                "custom_functions": False}):
            with _patch.dict("os.environ", {"GEMINI_API_KEY": "k"}):
                from harperbot import analyze_with_gemini, BotInternalError
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
        with _patch("harperbot.load_config", return_value={
                "provider": "gemini", "model": "m", "focus": "all",
                "max_diff_length": 4000, "max_output_tokens": 100,
                "prompt": "{num_files} {files_list} {diff_content} {focus_instruction}",
                "custom_functions": False}):
            with _patch.dict("os.environ", {"GEMINI_API_KEY": "k"}):
                from harperbot import analyze_with_gemini, BotInternalError
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
        with _patch("harperbot.load_config", return_value={
                "provider": "gemini", "model": "m", "focus": "all",
                "max_diff_length": 4000, "max_output_tokens": 100,
                "prompt": "{num_files} {files_list} {diff_content} {focus_instruction}",
                "custom_functions": False}):
            with _patch.dict("os.environ", {"GEMINI_API_KEY": "k"}):
                from harperbot import analyze_with_gemini, BotInternalError
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
        from harperbot import log_api_usage
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
        from harperbot import log_api_usage
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
        from harperbot import log_api_usage
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
            with patch("harperbot.load_config", return_value=self._config()):
                from harperbot import analyze_with_gemini
                msg = analyze_with_gemini(Mock(), self._details())
                self.assertIn("OPENROUTER_API_KEY is not set", msg)

    def test_success_returns_text_and_logs_resolved_model(self):
        import requests
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "k"}):
            with patch("harperbot.load_config", return_value=self._config()):
                with patch("harperbot.requests.post") as mp:
                    resp = Mock()
                    resp.json.return_value = {
                        "model": "openai/gpt-5.2",
                        "choices": [{"message": {"content": "looks good"}}]}
                    mp.return_value = resp
                    from harperbot import analyze_with_gemini
                    out = analyze_with_gemini(Mock(), self._details())
                    self.assertEqual(out, "looks good")
                    body = mp.call_args[1]["json"]
                    self.assertEqual(body["model"], "openrouter/auto")
                    self.assertEqual(body["messages"][0]["role"], "user")

    def test_auth_failure_maps_to_auth_message(self):
        import requests
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "k"}):
            with patch("harperbot.load_config", return_value=self._config()):
                with patch("harperbot.requests.post") as mp:
                    err_resp = Mock()
                    err_resp.status_code = 401
                    mp.side_effect = requests.HTTPError(response=err_resp)
                    from harperbot import analyze_with_gemini
                    msg = analyze_with_gemini(Mock(), self._details())
                    self.assertIn("Invalid API key or authentication failed", msg)
                    self.assertIn("openrouter", msg)

if __name__ == "__main__":
    unittest.main()
