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
    analyze_with_gemini,
    apply_suggestions_to_pr,
    create_branch,
    find_diff_position,
    load_config,
    parse_diff_for_suggestions,
    verify_webhook_signature,
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
            self.assertEqual(config["model"], "gemini-2.0-flash")
            self.assertEqual(config["max_diff_length"], 4000)
            self.assertEqual(config["temperature"], 0.2)
            self.assertEqual(config["max_output_tokens"], 4096)
            self.assertIn("prompt", config)  # Should include default prompt

    @patch("harperbot.genai.GenerativeModel")
    @patch("harperbot.load_config")
    def test_analyze_with_gemini_success(self, mock_load_config, mock_model_class):
        """Test successful Gemini analysis."""
        # Mock config with all required keys
        mock_load_config.return_value = {
            "model": "gemini-2.0-flash",
            "focus": "all",
            "max_diff_length": 4000,
            "temperature": 0.2,
            "max_output_tokens": 4096,
            "prompt": "Test prompt {num_files} {files_list} {diff_content} {focus_instruction}",
        }

        # Mock model and response
        mock_model = Mock()
        mock_response = Mock()
        mock_response.text = "Test analysis"
        mock_model.generate_content.return_value = mock_response
        mock_model_class.return_value = mock_model

        pr_details = {"title": "Test PR", "body": "Test body", "files_changed": ["test.py"], "diff": "test diff"}

        result = analyze_with_gemini(pr_details)
        self.assertEqual(result, "Test analysis")
        mock_model.generate_content.assert_called_once()

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


if __name__ == "__main__":
    unittest.main()
