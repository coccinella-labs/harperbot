# SPDX-License-Identifier: MIT
# Copyright (c) 2026 coccinella-labs

#!/usr/bin/env python3
"""
GitHub PR Bot that analyzes pull requests using Google's Gemini API.
Supports both CLI and webhook modes.
"""

import argparse
import ast
import hashlib
import hmac
import logging
import os
import re
import sys

import google.genai as genai
import yaml
from cerebras.cloud.sdk import Cerebras
from dotenv import load_dotenv
from github import Auth, Github
from google.genai import types
from harperbot_apply import handle_apply_comment

# Flask imported conditionally for webhook mode
flask_available = False
try:
    from flask import Flask, jsonify, request

    flask_available = True
    app = Flask(__name__)

    @app.route("/webhook", methods=["POST"])
    def webhook():
        return webhook_handler()

except ImportError:
    pass


def find_diff_position(diff, file_path, line_number):
    """
    Find the position in the diff hunk for a given file and line number.

    Parses the unified diff to locate the hunk containing the specified line,
    then calculates the position within that hunk for inline comments.
    """
    lines = diff.split("\n")
    i = 0
    while i < len(lines):
        # Look for the diff header for the specific file
        if lines[i].startswith("diff --git") and f"b/{file_path}" in lines[i]:
            i += 1  # Skip the header
            # Process hunks for this file
            while i < len(lines):
                if lines[i].startswith("@@"):
                    # Parse hunk header to get starting line in new file
                    match = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", lines[i])
                    if match:
                        hunk_start = int(match.group(1))
                        i += 1  # Move to hunk content
                        # Collect all lines in this hunk
                        hunk_lines = []
                        while i < len(lines) and not lines[i].startswith("@@") and not lines[i].startswith("diff --git"):
                            hunk_lines.append(lines[i])
                            i += 1
                        # Find the position of the target line in the hunk
                        # Simulate line numbers in the new file
                        current_line = hunk_start
                        position = 1
                        for line in hunk_lines:
                            if line.startswith("+"):
                                if current_line == line_number:
                                    return position
                                current_line += 1
                            elif line.startswith("-"):
                                # Removed line, no change to current_line
                                pass
                            else:
                                # Context line
                                current_line += 1
                            position += 1
                elif lines[i].startswith("diff --git"):
                    break
                else:
                    i += 1
        else:
            i += 1
    return None  # Line not found in any hunk


def setup_environment():
    """Load environment variables and configure the Gemini API."""
    load_dotenv()

    # Setup logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

    # Get GitHub token and API key from environment
    github_token = os.getenv("GITHUB_TOKEN")
    gemini_api_key = os.getenv("GEMINI_API_KEY")

    if not github_token or not gemini_api_key:
        logging.error("Missing required environment variables. Ensure GITHUB_TOKEN and GEMINI_API_KEY are set.")
        sys.exit(1)

    # Create Gemini client
    client = genai.Client(api_key=gemini_api_key)
    return github_token, client


def get_pr_details(github_token, repo_name, pr_number):
    """Fetch PR details from GitHub."""
    g = Github(auth=Auth.Token(github_token))
    repo = g.get_repo(repo_name)
    pr = repo.get_pull(pr_number)

    # Get PR details
    files_changed = [f.filename for f in pr.get_files()]
    diff_url = pr.diff_url

    # Get diff content
    import requests

    diff_content = requests.get(diff_url).text

    return {
        "title": pr.title,
        "body": pr.body or "",
        "author": pr.user.login,
        "files_changed": files_changed,
        "diff": diff_content,
        "base": pr.base.ref,
        "head": pr.head.ref,
        "head_sha": pr.head.sha,
        "number": pr_number,
    }


def load_config():
    """
    Load configuration from config.yaml with defaults.

    Supports customization of analysis focus, model, limits, and AI prompt.
    Users can modify config.yaml to change bot behavior without code changes.
    """
    default_prompt = """**Files Changed** ({num_files}):
{files_list}

```diff
{diff_content}
```

{focus_instruction}

Provide a concise code review analysis in this format:

## Summary
[Brief overview of changes and purpose]

### Scores
- Code Quality: [score]/10
- Maintainability: [score]/10
- Security: [score]/10

### Strengths
- [Key positives]
- [What's working well]

### Areas Needing Attention
- [Potential issues or improvements]
- [Be specific and constructive]

### Recommendations
- [Specific suggestions for code, docs, or tests]

### Code Suggestions
- [Provide specific code changes as diff blocks]
- [Use ```diff format for each suggestion]

### Next Steps
- [Actionable items for the author]"""

    default_config = {
        "focus": "all",
        "model": "gemini-2.0-flash",
        "max_diff_length": 4000,
        "temperature": 0.2,
        "max_output_tokens": 4096,
        "enable_authoring": False,
        "auto_commit_suggestions": False,
        "create_improvement_prs": False,
        "improvement_branch_pattern": "harperbot-improvements-{timestamp}",
        "prompt": default_prompt,
        "safety_settings": [
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
        ],
    }
    config_path = os.path.join(os.path.dirname(__file__), "config.yaml")
    if os.path.exists(config_path):
        with open(config_path, "r") as f:
            try:
                user_config = yaml.safe_load(f) or {}
                return {**default_config, **user_config}
            except yaml.YAMLError as e:
                logging.error(f"Error loading config.yaml: {e}")
                return default_config
    return default_config


def analyze_with_gemini(client, pr_details):
    """Analyze the PR using Gemini API."""
    try:
        config = load_config()
        model_name = config.get("model", "gemini-2.0-flash")
        focus = config.get("focus", "all")
        max_diff = config.get("max_diff_length", 4000)
        temperature = config.get("temperature", 0.2)
        max_output_tokens = config.get("max_output_tokens", 4096)
        safety_settings = config.get("safety_settings", [])
        provider = config.get("provider", "gemini")

        # Auto-select model based on PR complexity
        diff_length = len(pr_details["diff"])
        num_files = len(pr_details["files_changed"])
        if provider == "gemini":
            if diff_length > 10000 or num_files > 10:
                model_name = "gemini-2.5-flash"  # More powerful model for complex PRs
            else:
                model_name = config.get("model", "gemini-2.0-flash")
        elif provider == "cerebras":
            model_name = config.get("cerebras_model", "gpt-oss-120b")
        else:
            raise ValueError(f"Unsupported provider: {provider}")

        # Use client for selected model

        # Prepare the prompt based on focus
        focus_instructions = {
            "security": "Focus primarily on security concerns, authentication, data handling, and potential vulnerabilities.",
            "performance": "Focus primarily on performance optimizations, efficiency, and potential bottlenecks.",
            "quality": "Focus primarily on code quality, maintainability, readability, and best practices.",
        }
        focus_instruction = focus_instructions.get(focus, "")

        # Use configurable prompt template
        prompt_template = config["prompt"]
        formatted_prompt = prompt_template.format(
            num_files=len(pr_details["files_changed"]),
            files_list=", ".join(pr_details["files_changed"]),
            diff_content=pr_details["diff"][:max_diff],
            focus_instruction=focus_instruction,
        )

        if provider == "gemini":
            # Define custom tools
            def check_python_syntax(code: str) -> str:
                """Check Python code for syntax errors."""
                try:
                    ast.parse(code)
                    return "No syntax errors found."
                except SyntaxError as e:
                    return f"Syntax error: {e}"

            tools = [
                types.FunctionDeclaration(
                    name="check_python_syntax",
                    description="Check Python code for syntax errors",
                    parameters={
                        "type": "object",
                        "properties": {
                            "code": {
                                "type": "string",
                                "description": "The Python code to check",
                            }
                        },
                        "required": ["code"],
                    },
                )
            ]

            # Generate content with config
            response = client.models.generate_content(
                model=model_name,
                contents=formatted_prompt,
                config=types.GenerateContentConfig(
                    temperature=temperature,
                    top_p=0.95,
                    top_k=40,
                    max_output_tokens=max_output_tokens,
                    safety_settings=safety_settings,
                    tools=tools,
                ),
            )
        elif provider == "cerebras":
            # Use Cerebras client
            cerebras_client = Cerebras(api_key=os.environ.get("CEREBRAS_API_KEY"))
            response = cerebras_client.chat.completions.create(
                messages=[{"role": "user", "content": formatted_prompt}],
                model=model_name,
                max_tokens=max_output_tokens,
                temperature=temperature,
                top_p=0.95,
            )
            # Wrap response to mimic Gemini structure
            response.text = response.choices[0].message.content
            response.candidates = None  # No function calls for now

        # Check for function calls
        if response.candidates and response.candidates[0].content.parts:
            for part in response.candidates[0].content.parts:
                if hasattr(part, "function_call"):
                    logging.info(f"Function call detected: {part.function_call.name}")
                    # For now, just log; full handling would execute and send back

        # Handle different response formats
        def extract_text(resp):
            """
            Extract text from Gemini API response object.

            Attempts to extract text from various possible response structures:
            - Direct text attribute
            - Candidates with content parts
            - Direct parts array

            Args:
                resp: The response object from Gemini API

            Returns:
                str: Sanitized extracted text, or None if extraction fails
            """
            try:
                # Try the standard text accessor first
                if getattr(resp, "text", None):
                    logging.debug("Extracted text from direct response.text")
                    return sanitize_text(resp.text.strip())

                # Try candidates structure (most common for Gemini API)
                candidates = getattr(resp, "candidates", None)
                if candidates:
                    logging.debug(f"Found {len(candidates)} candidates")
                    for i, candidate in enumerate(candidates):
                        content = getattr(candidate, "content", None)
                        if content and getattr(content, "parts", None):
                            parts = [getattr(part, "text", "") for part in content.parts if getattr(part, "text", None)]
                            if parts:
                                logging.debug(f"Extracted text from candidate {i}")
                                return sanitize_text("\n".join(parts).strip())

                # Try direct parts access as fallback
                parts = getattr(resp, "parts", None)
                if parts:
                    parts = [getattr(part, "text", "") for part in parts if getattr(part, "text", None)]
                    if parts:
                        logging.debug("Extracted text from direct response.parts")
                        return sanitize_text("\n".join(parts).strip())

                logging.warning("No text found in any response structure")
            except Exception as extract_error:
                logging.error(f"Error during text extraction: {str(extract_error)}")
                return None

            return None

        def sanitize_text(text):
            """Comprehensive sanitization of extracted text for security."""
            if not text:
                return text
            # Remove potentially dangerous patterns
            text = re.sub(r"</?script[^>]*>", "", text, flags=re.IGNORECASE)
            text = re.sub(r"<[^>]+>", "", text)  # Remove all HTML tags
            text = re.sub(r"javascript:", "", text, flags=re.IGNORECASE)
            text = re.sub(r"on\w+\s*=", "", text, flags=re.IGNORECASE)  # Remove event handlers
            # Limit length to prevent abuse
            if len(text) > 10000:
                text = text[:10000] + "... (truncated for length)"
            return text.strip()

        try:
            text = extract_text(response)
            if text:
                return text

            # Check for finish reasons that indicate no content
            candidates = getattr(response, "candidates", None)
            if candidates:
                for candidate in candidates:
                    finish_reason = getattr(candidate, "finish_reason", None)
                    if finish_reason:
                        if "MAX_TOKENS" in str(finish_reason):
                            return "Analysis truncated due to token limit. The code changes are too extensive for a complete analysis. Please review manually or split into smaller PRs."
                        elif "SAFETY" in str(finish_reason):
                            return "Analysis blocked due to content safety filters. Please ensure the PR content complies with usage policies."
                        elif "STOP" in str(finish_reason):
                            return "Analysis completed but no content was generated. This may indicate an issue with the prompt or model."

            # If we get here, no text found - log and return safe message
            logging.warning(f"No text extracted from response. Response type: {type(response)}")
            return "Unable to generate analysis due to an unexpected response format. Please try again or review the code manually."

        except Exception as e:
            # Log the error and return safe info
            logging.error(f"Error processing Gemini response: {str(e)}")
            return f"Error processing response: {str(e)}\n\nResponse type: {type(response)}"

    except Exception as e:
        error_msg = str(e).lower()
        context = (
            f" (PR: {pr_details.get('title', 'Unknown')}, Model: {model_name}, Diff length: {len(pr_details.get('diff', ''))})"
        )
        if "quota" in error_msg or "rate limit" in error_msg or "billing" in error_msg:
            logging.error(f"API quota/rate limit error{context}: {str(e)}")
            return f"Error generating analysis: API quota exceeded{context}. Please check your billing or try again later."
        elif "api key" in error_msg or "authentication" in error_msg or "unauthorized" in error_msg:
            logging.error(f"API authentication error{context}: {str(e)}")
            return f"Error generating analysis: Invalid API key or authentication failed{context}. Please check your GEMINI_API_KEY."
        elif "model" in error_msg or "not found" in error_msg:
            logging.error(f"Model error{context}: {str(e)}")
            return f"Error generating analysis: Requested model not available{context}. Please try again later."
        else:
            logging.error(f"Unexpected API error{context}: {str(e)}")
            return f"Error generating analysis: API unavailable{context}. Please try again later."


def parse_diff_for_suggestions(diff_text):
    """Parse a diff block to extract file, line, and suggestion code."""
    lines = diff_text.strip().split("\n")
    if not lines or not lines[0].startswith("--- a/"):
        return None
    file_path = lines[0][6:]  # --- a/file
    hunk_start = None
    suggestion_lines = []
    current_line = 0
    line_num = 0
    for line in lines:
        if line.startswith("@@"):
            match = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
            if match:
                hunk_start = int(match.group(1))
                current_line = hunk_start
        elif hunk_start is not None:
            if line.startswith("+"):
                if not suggestion_lines:  # First + line
                    line_num = current_line
                suggestion_lines.append(line[1:])  # remove +
                current_line += 1
            elif line.startswith("-"):
                # Removed line, no change to current_line
                pass
            else:
                # Context line
                current_line += 1
    if suggestion_lines:
        return file_path, line_num, "\n".join(suggestion_lines)
    return None


def format_comment(analysis):
    """Format the analysis with proper markdown and emojis."""
    return f"""[![HarperBot](https://github.com/coccinella-labs/harperbot/actions/workflows/harperbot.yml/badge.svg)](https://github.com/coccinella-labs/harperbot/actions/workflows/harperbot.yml)

<details>
<summary>HarperBot Analysis</summary>

{analysis}

</details>

---"""


def parse_code_suggestions(analysis):
    """
    Parse code suggestions from analysis text.

    Extracts diff blocks and parses them into (file_path, line, suggestion) tuples.
    """
    diff_blocks = []
    start_pos = 0
    while True:
        start_pos = analysis.find("```diff\n", start_pos)
        if start_pos == -1:
            break
        end_pos = analysis.find("\n```", start_pos + 8)
        if end_pos == -1:
            break
        diff_text = analysis[start_pos + 8 : end_pos]
        diff_blocks.append(diff_text)
        start_pos = end_pos + 4
    suggestions = []
    for diff_text in diff_blocks:
        parsed = parse_diff_for_suggestions(diff_text)
        if parsed:
            file_path, line, suggestion = parsed
            suggestions.append((file_path, str(line), suggestion))
    return suggestions


def create_branch(repo, base_branch, new_branch_name):
    """
    Create a new branch from the base branch.

    Args:
        repo: GitHub repository object
        base_branch: Name of the base branch (e.g., 'main')
        new_branch_name: Name for the new branch

    Returns:
        The created branch reference
    """
    try:
        # Check if branch already exists
        try:
            existing_ref = repo.get_git_ref(f"heads/{new_branch_name}")
            logging.warning(f"Branch '{new_branch_name}' already exists, using existing")
            return existing_ref
        except Exception:
            pass  # Branch doesn't exist, create it

        base_ref = repo.get_git_ref(f"heads/{base_branch}")
        repo.create_git_ref(ref=f"refs/heads/{new_branch_name}", sha=base_ref.object.sha)
        logging.info(f"Created branch '{new_branch_name}' from '{base_branch}'")
        return repo.get_git_ref(f"heads/{new_branch_name}")
    except Exception as e:
        logging.error(f"Error creating branch '{new_branch_name}': {str(e)}")
        raise


def create_commit_with_changes(repo, branch_ref, changes, commit_message):
    """
    Create a commit with the given file changes.

    Args:
        repo: GitHub repository object
        branch_ref: Branch reference to commit to
        changes: Dict of {file_path: new_content}
        commit_message: Commit message

    Returns:
        The created commit
    """
    try:
        # Get the current tree
        current_commit = repo.get_git_commit(branch_ref.object.sha)
        current_tree = repo.get_git_tree(current_commit.sha)

        # Create blobs for new/updated files
        new_blobs = []
        for file_path, content in changes.items():
            blob = repo.create_git_blob(content, "utf-8")
            new_blobs.append({"path": file_path, "mode": "100644", "type": "blob", "sha": blob.sha})  # Regular file

        # Create new tree
        tree = repo.create_git_tree(new_blobs, base_tree=current_tree)
        author = {
            "name": "HarperBot",
            "email": "236089746+harper-bot-glitch@users.noreply.github.com",
        }
        commit = repo.create_git_commit(commit_message, tree, [current_commit], author=author)
        branch_ref.edit(commit.sha)
        logging.info(f"Created commit with {len(changes)} file changes")
        return commit
    except Exception as e:
        logging.error(f"Error creating commit: {str(e)}")
        raise


def create_improvement_pr(repo, head_branch, base_branch, title, body):
    """
    Create a pull request with improvements.

    Args:
        repo: GitHub repository object
        head_branch: Branch with improvements
        base_branch: Target branch
        title: PR title
        body: PR description

    Returns:
        The created pull request
    """
    try:
        pr = repo.create_pull(title=title, body=body, head=head_branch, base=base_branch)
        logging.info(f"Created improvement PR #{pr.number}: {title}")
        return pr
    except Exception as e:
        logging.error(f"Error creating PR: {str(e)}")
        raise


def apply_suggestions_to_pr(repo, pr, suggestions):
    """
    Apply code suggestions directly to the PR branch.

    Args:
        repo: GitHub repository object
        pr: Pull request object
        suggestions: List of (file_path, line, suggestion) tuples
    """
    try:
        from collections import defaultdict

        # Get PR head branch
        head_ref = repo.get_git_ref(f"heads/{pr.head.ref}")

        # Group suggestions by file
        suggestion_groups = defaultdict(list)
        for file_path, line, suggestion in suggestions:
            suggestion_groups[file_path].append((int(line), suggestion))

        # Apply suggestions per file
        changes = {}
        for file_path, suggs in suggestion_groups.items():
            # Get current file content
            try:
                file_content = repo.get_contents(file_path, ref=pr.head.sha)
                current_content = file_content.decoded_content.decode("utf-8")
            except Exception:
                # File doesn't exist, create it
                current_content = ""

            lines = current_content.split("\n")

            # Sort suggestions by line number
            suggs.sort(key=lambda x: x[0])

            offset = 0
            applied = False
            for line, suggestion in suggs:
                adjusted_line = line - 1 + offset  # Convert to 0-based and adjust for previous changes

                if not (0 <= adjusted_line < len(lines)):
                    logging.warning(
                        f"Suggestion for {file_path}:{line} is out of bounds (adjusted line {adjusted_line}), skipping"
                    )
                    continue

                sugg_lines = suggestion.split("\n")
                num_old = 1  # Assume replacing 1 line (simplified; full diff parsing needed for accurate replacements)
                num_new = len(sugg_lines)

                lines = lines[:adjusted_line] + sugg_lines + lines[adjusted_line + num_old :]
                offset += num_new - num_old
                applied = True

            if applied:
                changes[file_path] = "\n".join(lines)

        if changes:
            create_commit_with_changes(
                repo,
                head_ref,
                changes,
                "Apply code suggestions from HarperBot analysis",
            )
            logging.info(f"Applied {len(suggestion_groups)} file changes to PR #{pr.number}")
    except Exception as e:
        logging.error(f"Error applying suggestions to PR: {str(e)}")


def create_improvement_pr_from_analysis(repo, pr_details, analysis, config):
    """
    Create an improvement PR with additional suggestions beyond the original PR.

    Args:
        repo: GitHub repository object
        pr_details: PR details dict
        analysis: Full analysis text
        config: Configuration dict
    """
    try:
        import time

        timestamp = str(int(time.time()))

        # Generate branch name
        branch_pattern = config.get("improvement_branch_pattern", "harperbot-improvements-{timestamp}")
        branch_name = branch_pattern.replace("{timestamp}", timestamp).replace("{pr_number}", str(pr_details["number"]))

        # Create branch from main/master
        base_branch = pr_details.get("base", "main")
        branch_ref = create_branch(repo, base_branch, branch_name)

        # Create an initial empty commit to allow PR creation
        create_commit_with_changes(repo, branch_ref, {}, "Initial commit for HarperBot improvements")

        # For now, create an empty improvement PR (could be extended to include actual improvements)
        title = f"HarperBot Improvements for PR #{pr_details['number']}"
        body = f"""## HarperBot Improvement Suggestions

This PR contains additional improvements suggested by HarperBot analysis of PR #{pr_details["number"]}.

### Analysis Summary
{analysis[:1000]}...

---
*Generated by HarperBot*"""

        create_improvement_pr(repo, branch_name, base_branch, title, body)

    except Exception as e:
        logging.error(f"Error creating improvement PR: {str(e)}")


def update_main_comment(analysis):
    """
    Update the main comment by replacing the code suggestions section.
    """
    start_pos = analysis.find("### Code Suggestions\n")
    if start_pos == -1:
        return analysis
    end_pos = analysis.find("###", start_pos + 21)
    if end_pos == -1:
        end_pos = len(analysis)
    return analysis[:start_pos] + "### Code Suggestions\n- Suggestions posted as inline comments below.\n" + analysis[end_pos:]


def save_inline_suggestions(pr_details, suggestions):
    """
    Save inline code suggestions to a JSON file for workflow posting.
    """
    try:
        import json

        suggestions_data = []
        for file_path, line, suggestion in suggestions:
            try:
                line_num = int(line)
            except (ValueError, TypeError):
                logging.warning(f"Invalid line number format '{line}' for suggestion in '{file_path}'. Skipping.")
                continue

            position = find_diff_position(pr_details["diff"], file_path, line_num)
            if position is not None:
                suggestions_data.append(
                    {
                        "path": file_path,
                        "position": position,
                        "body": f"```suggestion\n{suggestion}\n```",
                    }
                )
        if suggestions_data:
            with open("suggestions.json", "w") as f:
                json.dump(suggestions_data, f)
            logging.info(f"Saved {len(suggestions_data)} inline suggestions to suggestions.json")
        else:
            logging.info("No valid inline suggestions to save")
    except Exception as e:
        logging.error(f"Error saving suggestions: {str(e)}")


def post_inline_suggestions(pr, pr_details, suggestions, github_token, repo):
    """
    Post inline code suggestions as a pull request review.
    """
    try:
        commit = repo.get_commit(pr_details["head_sha"])
        review_comments = []
        for file_path, line, suggestion in suggestions:
            try:
                line_num = int(line)
            except (ValueError, TypeError):
                logging.warning(f"Invalid line number format '{line}' for suggestion in '{file_path}'. Skipping.")
                continue

            position = find_diff_position(pr_details["diff"], file_path, line_num)
            if position is not None:
                body = f"```suggestion\n{suggestion}\n```"
                review_comments.append({"path": file_path, "position": position, "body": body})
        if review_comments:
            pr.create_review(commit=commit, comments=review_comments, event="COMMENT")
            logging.info(f"Posted {len(review_comments)} inline suggestions")
        else:
            logging.info("No valid inline suggestions to post")
    except Exception as e:
        logging.error(f"Error posting review with suggestions: {str(e)}")
        # Don't fail the whole process for review posting errors


def verify_webhook_signature(payload, signature, secret):
    """
    Verify GitHub webhook signature for security.

    Uses HMAC-SHA256 to ensure the webhook payload hasn't been tampered with.
    This prevents malicious requests from triggering analysis.

    Args:
        payload: Raw request body bytes
        signature: GitHub signature header (sha256=...)
        secret: Webhook secret configured in GitHub App

    Returns:
        bool: True if signature is valid
    """
    if not signature or not secret:
        return False
    sha_name, sig = signature.split("=", 1)
    if sha_name != "sha256":
        return False
    mac = hmac.new(secret.encode(), msg=payload, digestmod=hashlib.sha256)
    return hmac.compare_digest(mac.hexdigest(), sig)


def setup_environment_webhook(installation_id):
    """
    Setup environment for webhook mode using GitHub App authentication.

    Generates an installation token for the specific repository installation.
    This provides secure, scoped access without storing long-lived tokens.
    """
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

    gemini_api_key = os.getenv("GEMINI_API_KEY")
    app_id = os.getenv("HARPER_BOT_APP_ID")
    private_key = os.getenv("HARPER_BOT_PRIVATE_KEY")

    if not gemini_api_key or not app_id or not private_key:
        logging.error("Missing required environment variables for webhook mode")
        raise ValueError("Missing required environment variables")

    client = genai.Client(api_key=gemini_api_key)

    # Generate installation-specific token
    auth = Auth.AppAuth(app_id, private_key)
    installation_auth = auth.get_installation_auth(installation_id)
    g = Github(auth=installation_auth)
    return g, installation_auth.token, client


def get_pr_details_webhook(g, repo_name, pr_number):
    """Fetch PR details using GitHub App authentication."""
    repo = g.get_repo(repo_name)
    pr = repo.get_pull(pr_number)

    files_changed = [f.filename for f in pr.get_files()]

    diff_content = pr.get_diff()

    return {
        "title": pr.title,
        "body": pr.body or "",
        "author": pr.user.login,
        "files_changed": files_changed,
        "diff": diff_content,
        "base": pr.base.ref,
        "head": pr.head.ref,
        "head_sha": pr.head.sha,
        "number": pr_number,
    }


def post_comment_webhook(github_token: str, repo_name: str, pr_details: dict, analysis: str):
    """
    Post analysis comment and inline suggestions using GitHub App auth.

    Creates a main comment with the analysis summary, and posts
    code suggestions as inline review comments. Optionally applies
    suggestions directly or creates improvement PRs.
    """
    try:
        g = Github(github_token)
        config = load_config()
        repo = g.get_repo(repo_name)
        pr = repo.get_pull(pr_details["number"])

        suggestions = parse_code_suggestions(analysis)
        main_comment = update_main_comment(analysis)

        # Post main analysis comment
        formatted_comment = format_comment(main_comment)
        pr.create_issue_comment(formatted_comment)
        logging.info(f"Posted main analysis comment to PR #{pr_details['number']}")

        # Post or save inline suggestions based on config
        post_in_yaml = config.get("post_suggestions_in_yaml", True)
        if post_in_yaml:
            save_inline_suggestions(pr_details, suggestions)
        else:
            post_inline_suggestions(pr, pr_details, suggestions, github_token, repo)

        # Apply authoring features if enabled
        if config.get("enable_authoring", False):
            if config.get("auto_commit_suggestions", False) and suggestions:
                apply_suggestions_to_pr(repo, pr, suggestions)

            if config.get("create_improvement_prs", False):
                create_improvement_pr_from_analysis(repo, pr_details, analysis, config)

    except Exception as e:
        logging.error(f"Error posting comment to PR #{pr_details.get('number', 'unknown')}: {str(e)}")
        raise


def webhook_handler():
    """
    Handle incoming GitHub webhooks for PR events.

    Processes webhook payloads for pull request opened/synchronize/reopened events.
    Verifies signature, extracts PR data, runs analysis, and posts comments.
    """
    if not flask_available:
        logging.error("Flask not available for webhook mode")
        return {"error": "Flask not installed"}, 500

    payload = request.get_data()
    signature = request.headers.get("X-Hub-Signature-256")
    secret = os.getenv("WEBHOOK_SECRET")

    if not verify_webhook_signature(payload, signature, secret):
        logging.warning("Invalid webhook signature received")
        return jsonify({"error": "Invalid signature"}), 403

    data = request.get_json()

    event_type = data.get("action")
    has_pr = "pull_request" in data
    has_comment = "issue" in data and "comment" in data

    installation_id = data["installation"]["id"]
    repo_name = data["repository"]["full_name"]

    if event_type == "created" and has_comment:
        issue = data["issue"]
        if "pull_request" not in issue:
            return jsonify({"status": "ignored"})  # Not a PR comment
        pr_number = issue["number"]
        comment_body = data["comment"]["body"].strip()
        if comment_body.lower() == "/apply":
            return handle_apply_comment(installation_id, repo_name, pr_number)
        else:
            return jsonify({"status": "ignored"})

    # Only process PR events
    if event_type not in ["opened", "synchronize", "reopened"] or not has_pr:
        logging.info(f"Ignored webhook event: action={event_type}, has_pr={has_pr}")
        return jsonify({"status": "ignored"})

    pr_number = data["pull_request"]["number"]

    logging.info(f"Processing PR #{pr_number} in {repo_name}")

    try:
        g, installation_token, client = setup_environment_webhook(installation_id)
        pr_details = get_pr_details_webhook(g, repo_name, pr_number)
        analysis = analyze_with_gemini(client, pr_details)
        post_comment_webhook(installation_token, repo_name, pr_details, analysis)
        logging.info(f"Successfully processed PR #{pr_number}")
        return jsonify({"status": "ok"})
    except Exception as e:
        logging.error(f"Error processing webhook: {str(e)}")
        return jsonify({"error": "Processing failed"}), 500


def main():
    """Main function to run the PR bot."""
    # Parse command line arguments
    parser = argparse.ArgumentParser(description="GitHub PR Bot with Gemini AI")
    parser.add_argument("--repo", required=True, help="GitHub repository in format: owner/repo")
    parser.add_argument("--pr", type=int, required=True, help="Pull request number")
    args = parser.parse_args()

    # Setup environment and get PR details
    github_token, client = setup_environment()
    pr_details = get_pr_details(github_token, args.repo, args.pr)

    # Analyze PR with Gemini
    logging.info("Analyzing PR with Gemini...")
    analysis = analyze_with_gemini(client, pr_details)
    logging.debug("Analysis response received")
    logging.debug(analysis)

    # Post the comment with formatted analysis
    logging.info("Posting analysis to PR...")
    try:
        post_comment_webhook(github_token, args.repo, pr_details, analysis)
        logging.info("Analysis complete!")
    except Exception as e:
        logging.error(f"Failed to post analysis: {str(e)}")
        # Continue even if posting fails


if __name__ == "__main__":
    if len(sys.argv) > 1:
        # CLI mode
        main()
    else:
        # Webhook mode
        if flask_available:
            print("Starting HarperBot in webhook mode...")
            # Note: Flask's development server is for testing only. For production,
            # use a WSGI server like Gunicorn: gunicorn -w 4 harperbot:app
            app.run(debug=False)
        else:
            print("Flask not installed. For webhook mode, install with: pip install flask")
            print("For CLI mode, run: python harperbot.py --repo owner/repo --pr 123")
            sys.exit(1)
