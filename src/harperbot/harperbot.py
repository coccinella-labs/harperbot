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
from types import SimpleNamespace

import requests

import google.genai as genai
import yaml
from cerebras.cloud.sdk import Cerebras
from dotenv import load_dotenv
from github import Auth, Github
from google.genai import types
try:
    from .harperbot_apply import handle_apply_comment
except ImportError:  # executed directly as a script
    from harperbot_apply import handle_apply_comment

try:
    from . import output as _output
except ImportError:  # executed directly as a script
    import output as _output

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

Review discipline (these override any conflicting habit):
- Judge only what the diff shows. If context is missing for a judgment,
  say so instead of guessing.
- Report problems only: bugs, risks, missing tests, security issues.
  An empty findings list is a valid answer. Do not pad with praise or
  restate the change as an achievement.
- Every finding needs severity, the exact conditions that trigger it,
  and evidence (file, line, the code in question). Do not flag what the
  diff cannot support.
- Style nits only if they cause real harm.

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
- [Actionable items for the author]

### Structured findings (machine-readable, after the prose above)
End your response with a fenced JSON block listing each concrete problem
found, so counts and inline comments can be derived without parsing prose:
```json
{{"summary": "one-paragraph overall assessment",
 "findings": [{{"severity": "critical|high|medium|low|info",
   "category": "bug|security|performance|quality|style",
   "path": "relative/file.py", "line": 42,
   "message": "what is wrong, why, and the exact conditions that trigger it",
   "suggestion": "optional concrete fix"}}]}}
```
An empty "findings" list is valid when nothing is wrong. Keep the
human-readable review above; the JSON mirrors its actionable items."""

    default_config = {
        "focus": "all",
        # Request custom function tools from the model. Off by default:
        # function calls are never answered yet, and requesting tools
        # diverts responses into function calls the bot drops.
        "custom_functions": False,
        "model": "gemini-3.8-flash",
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


class BotInternalError(Exception):
    """Unexpected bot failure, as opposed to a provider error.

    Carries the failing step. Callers post a short notice and exit
    non-zero; raw detail stays in the run log only.
    """

    def __init__(self, step, public_message=None):
        self.step = step
        self.public_message = public_message
        super().__init__(public_message or f"Bot error during {step}")


def workflow_run_url():
    """Actions run URL, or None outside Actions (e.g. webhook mode)."""
    server = os.environ.get("GITHUB_SERVER_URL")
    repo = os.environ.get("GITHUB_REPOSITORY")
    run_id = os.environ.get("GITHUB_RUN_ID")
    if server and repo and run_id:
        return f"{server}/{repo}/actions/runs/{run_id}"
    return None


try:
    import httpx as _httpx

    _HTTPX_TRANSPORT_ERRORS = (_httpx.TransportError,)
except ImportError:  # pragma: no cover - httpx ships with google-genai
    _HTTPX_TRANSPORT_ERRORS = ()


def _is_transient_transport(error, code):
    """True for timeouts and connection failures, which carry no status.

    Builtin TimeoutError/ConnectionError cover sockets and most HTTP
    stacks. httpx.TransportError covers its whole transport subtree
    (timeouts, network errors, protocol errors such as a server that
    disconnects mid-response). The type-name fallback covers SDK transport
    errors such as Cerebras APITimeoutError/APIConnectionError, which are
    neither. Matching is on the exception type, never on message text.
    A network blip must read as transient, not as a bot bug.
    """
    if code is not None:
        return False
    if isinstance(error, (TimeoutError, ConnectionError)):
        return True
    if _HTTPX_TRANSPORT_ERRORS and isinstance(error, _HTTPX_TRANSPORT_ERRORS):
        return True
    typename = type(error).__name__.lower()
    return "timeout" in typename or "connect" in typename


def _empty_response_notice(response):
    msg = f"Model returned no text ({describe_empty_response(response)})."
    link = workflow_run_url()
    if link:
        msg += f" See the workflow run: {link}."
    return msg


def describe_empty_response(response):
    """Known facts about a textless response: finish reasons, block reason,
    part kinds. States what is in the response without asserting a cause."""
    reasons = []
    kinds = []
    for cand in getattr(response, "candidates", None) or []:
        fr = getattr(cand, "finish_reason", None)
        if fr is not None and str(fr) not in reasons:
            reasons.append(str(fr))
        for part in getattr(getattr(cand, "content", None), "parts", None) or []:
            if getattr(part, "text", None):
                kind = "text"
            elif getattr(part, "function_call", None) is not None:
                kind = "function_call"
            else:
                kind = "other"
            if kind not in kinds:
                kinds.append(kind)
    block = getattr(getattr(response, "prompt_feedback", None),
                    "block_reason", None)
    bits = []
    if reasons:
        bits.append("finish reason: " + ", ".join(reasons))
    if block:
        bits.append("block reason: " + str(block))
    bits.append("parts: " + (", ".join(kinds) if kinds else "none"))
    return "; ".join(bits)


def log_api_usage(response, model_name, provider):
    """Log token counts and finish reason. Counts only, never content.

    Fully defensive: usage shapes differ per provider and can be absent
    on errors or blocked responses. A logging failure must never fail
    a review, so the whole body is guarded.
    """
    try:
        usage = getattr(response, "usage_metadata", None) or getattr(
            response, "usage", None)

        def _count(*names):
            for name in names:
                value = getattr(usage, name, None) if usage is not None else None
                if isinstance(value, int):
                    return value
            return None

        prompt = _count("prompt_token_count", "prompt_tokens")
        candidates = _count("candidates_token_count", "completion_tokens")
        thinking = _count("thoughts_token_count", "thinking_tokens")
        total = _count("total_token_count", "total_tokens")
        finish = None
        for cand in getattr(response, "candidates", None) or []:
            finish = getattr(cand, "finish_reason", None)
            if finish is not None:
                break
        if finish is None:
            for choice in getattr(response, "choices", None) or []:
                finish = getattr(choice, "finish_reason", None)
                if finish is not None:
                    break
        logging.info(
            f"API usage model={model_name} provider={provider} "
            f"prompt={prompt} candidates={candidates} thinking={thinking} "
            f"total={total} finish={finish}")
    except Exception as e:
        logging.debug(f"Usage logging failed (non-fatal): {e}")


def classify_provider_error(error, context):
    """Map a provider SDK failure to a safe public message.

    Classification is structural: HTTP status read from the exception
    object, never message-text matching. Anything unrecognized raises
    BotInternalError, which callers report as a bot error and exit
    non-zero. Raw detail is logged here, in the run log only.
    """
    if isinstance(error, BotInternalError):
        raise error
    code = getattr(error, "code", getattr(error, "status_code", None))
    if _is_transient_transport(error, code):
        logging.warning(f"API transport error{context}: {error}")
        return (f"Error generating analysis: API temporarily unreachable{context}. "
                f"Please try again later.")
    if code in (400, 404):
        logging.error(f"API model error{context}: {error}")
        return (f"Error generating analysis: Requested model not available{context}. "
                f"Please check the configured model.")
    if code in (401, 403):
        logging.error(f"API authentication error{context}: {error}")
        return ("Error generating analysis: Invalid API key or authentication"
                f" failed{context}. Please check the provider API key.")
    if code == 429:
        logging.error(f"API quota/rate limit error{context}: {error}")
        return (f"Error generating analysis: API quota exceeded{context}. "
                f"Please check billing or try again later.")
    if isinstance(code, int) and 500 <= code < 600:
        logging.error(f"API server error{context}: {error}")
        return (f"Error generating analysis: API unavailable{context}. "
                f"Please try again later.")
    logging.exception(f"Unexpected bot error during analysis{context}")
    raise BotInternalError("analysis") from error


def generate_openrouter(api_key, model_name, formatted_prompt, max_output_tokens):
    """Call OpenRouter chat completions. Returns (content, resolved_model).

    Raises requests.HTTPError with the response attached on HTTP failures,
    so callers can map status codes structurally.
    """
    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json",
                 "HTTP-Referer": "https://github.com/coccinella-labs/harperbot",
                 "X-Title": "HarperBot"},
        json={"model": model_name,
              "messages": [{"role": "user",
                            "content": formatted_prompt}],
              "max_tokens": max_output_tokens},
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    resolved = data.get("model", model_name)
    logging.info(f"OpenRouter resolved model: {resolved}")
    return data["choices"][0]["message"]["content"], resolved


def _should_fallback_to_openrouter(error):
    """True when a Gemini failure is traffic-class and worth retrying.

    Only 429, 5xx, and transport errors qualify. Auth failures, missing
    keys, and unknown models are configuration or caller problems that a
    second provider will not fix, so they never trigger a fallback.
    """
    code = getattr(error, "code", getattr(error, "status_code", None))
    if code == 429:
        return True
    if isinstance(code, int) and 500 <= code < 600:
        return True
    return _is_transient_transport(error, code)


def analyze_with_gemini(client, pr_details):
    """Analyze the PR using Gemini API."""
    try:
        config = load_config()
        model_name = config.get("model", "gemini-3.8-flash")
        focus = config.get("focus", "all")
        max_diff = config.get("max_diff_length", 4000)
        temperature = config.get("temperature", 0.2)
        max_output_tokens = config.get("max_output_tokens", 4096)
        safety_settings = config.get("safety_settings", [])
        provider = config.get("provider", "gemini")
        required_key = {"gemini": "GEMINI_API_KEY",
                        "cerebras": "CEREBRAS_API_KEY",
                        "openrouter": "OPENROUTER_API_KEY"}[provider]
        if not os.environ.get(required_key):
            return (f"{required_key} is not set for provider {provider}. "
                    f"Add it as a repository secret; no review was performed.")

        # Auto-select model based on PR complexity
        diff_length = len(pr_details["diff"])
        num_files = len(pr_details["files_changed"])
        if provider == "gemini":
            if diff_length > 10000 or num_files > 10:
                model_name = "gemini-3.8-flash"  # Single model for all PR sizes for now
            else:
                model_name = config.get("model", "gemini-3.8-flash")
        elif provider == "cerebras":
            model_name = config.get("cerebras_model", "gpt-oss-120b")
        elif provider == "openrouter":
            # Pinned default; openrouter/auto without an allow-list would
            # route unrestricted, which is a separate, unbuilt feature.
            model_name = config.get("openrouter_model", "openrouter/auto")
        else:
            raise ValueError(f"Unsupported provider: {provider}")
        pr_details["model_name"] = model_name

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
            # Function calls are never answered (no execute-and-send-back
            # loop yet), so tools stay off unless explicitly enabled.
            tools = None
            if config.get("custom_functions", False):
                # Define custom tools
                def check_python_syntax(code: str) -> str:
                    """Check Python code for syntax errors."""
                    try:
                        ast.parse(code)
                        return "No syntax errors found."
                    except SyntaxError as e:
                        return f"Syntax error: {e}"

                tools = [
                    types.Tool(
                        function_declarations=[
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
                        ],
                    )
                ]

            # Generate content with config. On traffic-class failures,
            # fall back to OpenRouter once when its key is configured, so
            # one provider lagging does not sink the review.
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=formatted_prompt,
                    config=types.GenerateContentConfig(
                    thinking_config=types.ThinkingConfig(
                        thinking_level="medium"
                    ),
                    max_output_tokens=max_output_tokens,
                    safety_settings=safety_settings,
                    tools=tools,
                    ),
                )
            except Exception as gemini_error:
                or_key = os.environ.get("OPENROUTER_API_KEY")
                if or_key and _should_fallback_to_openrouter(gemini_error):
                    or_model = config.get("openrouter_model", "openrouter/auto")
                    logging.warning(
                        f"Gemini failed ({type(gemini_error).__name__}), "
                        f"trying OpenRouter model {or_model}")
                    try:
                        or_content, or_resolved = generate_openrouter(
                            or_key, or_model, formatted_prompt,
                            max_output_tokens)
                        pr_details["model_name"] = or_resolved
                    except Exception as or_error:
                        logging.error(f"OpenRouter fallback also failed: {or_error}")
                        return ("Error generating analysis: API unavailable "
                                f"(provider gemini model {model_name} and "
                                f"OpenRouter model {or_model} both failed). "
                                f"Please try again later.")
                    response = SimpleNamespace(text=or_content, candidates=None)
                else:
                    raise
            log_api_usage(response, model_name, provider)
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
            log_api_usage(response, model_name, provider)
            # Wrap response to mimic Gemini structure
            response.text = response.choices[0].message.content
            response.candidates = None  # No function calls for now
        elif provider == "openrouter":
            api_key = os.environ.get("OPENROUTER_API_KEY")
            try:
                content, resolved = generate_openrouter(
                    api_key, model_name, formatted_prompt, max_output_tokens)
                pr_details["model_name"] = resolved
            except requests.HTTPError as e:
                code = (e.response.status_code
                        if e.response is not None else None)
                if code in (401, 403):
                    return ("Error generating analysis: Invalid API key or "
                            f"authentication failed (provider openrouter, model "
                            f"{model_name}). Please check OPENROUTER_API_KEY.")
                if code in (402, 429):
                    return ("Error generating analysis: API quota exceeded "
                            f"(provider openrouter, model {model_name}). "
                            f"Please check billing or try again later.")
                raise
            response = SimpleNamespace(text=content, candidates=None)

        # Check for function calls
        if response.candidates and response.candidates[0].content.parts:
            for part in response.candidates[0].content.parts:
                call = getattr(part, "function_call", None)
                if call is not None:
                    logging.info(
                        f"Function call detected: {getattr(call, 'name', '?')}")
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
                            raise BotInternalError(
                                "analysis",
                                public_message=_empty_response_notice(response))

            # No text and no recognized finish reason: describe, don't diagnose.
            raise BotInternalError(
                "analysis", public_message=_empty_response_notice(response))

        except BotInternalError:
            raise
        except Exception as e:
            # Log the error and return safe info
            logging.error(f"Error processing Gemini response: {str(e)}")
            return f"Error processing response: {str(e)}\n\nResponse type: {type(response)}"

    except Exception as e:
        context = (
            f" (PR: {pr_details.get('title', 'Unknown')}, Model: {model_name}, Diff length: {len(pr_details.get('diff', ''))})"
        )
        return classify_provider_error(e, context)


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


ANALYSIS_MARKER = "<!-- harperbot-analysis -->"

# Matches bot comments written before the marker existed, so the first
# upsert run adopts them instead of leaving them orphaned.
LEGACY_ANALYSIS_SIGNATURE = "<summary>HarperBot Analysis</summary>"


def find_existing_analysis_comments(pr):
    """Return earlier bot analysis comments on this PR, oldest first."""
    matches = []
    for comment in pr.get_issue_comments():
        body = comment.body or ""
        if _output.parse_marker(body) is not None or (
            ANALYSIS_MARKER in body
            or (
                LEGACY_ANALYSIS_SIGNATURE in body
                and "harperbot/actions/workflows" in body
            )
        ):
            matches.append(comment)
    return matches


def upsert_analysis_comment(pr, formatted_comment):
    """Update the existing analysis comment, or post one if none exists.

    One comment per PR no matter how many times the bot runs. Any extra
    matches beyond the first are deleted, so concurrent runs converge
    instead of accumulating.
    """
    existing = find_existing_analysis_comments(pr)
    if not existing:
        pr.create_issue_comment(formatted_comment)
        logging.info(f"Posted main analysis comment to PR #{pr.number}")
        return
    existing[0].edit(formatted_comment)
    logging.info(f"Updated main analysis comment to PR #{pr.number}")
    for stale in existing[1:]:
        try:
            stale.delete()
        except Exception as e:
            logging.warning(f"Could not delete stale analysis comment: {e}")


def build_review_comment(analysis, pr_details, repo_name, config):
    """Assemble the sticky review comment through one pipeline.

    Returns (body, ok). ok=False means the findings block was missing
    or invalid, and the body is the raw-analysis fallback.
    """
    commit_sha = pr_details.get("head_sha", "")
    commit_url = f"https://github.com/{repo_name}/commit/{commit_sha}"
    model = pr_details.get("model_name") or config.get("model", "?")
    focus = config.get("focus", "all")
    run_url = workflow_run_url()
    summary, findings, ok = _output.parse_findings(analysis)
    if ok:
        return _output.format_review_comment(
            analysis=analysis, findings=findings, commit_sha=commit_sha,
            commit_url=commit_url, model=model, run_url=run_url,
            focus=focus), True
    return _output.format_fallback_comment(
        analysis=analysis, commit_sha=commit_sha, commit_url=commit_url,
        model=model, run_url=run_url, focus=focus), False


def record_failure(pr, attempted_sha, run_url):
    """Failed run: banner-update a prior review, or post a plain notice."""
    for comment in pr.get_issue_comments():
        body = comment.body or ""
        if _output.parse_marker(body) is not None or _output.find_legacy(body):
            comment.edit(_output.apply_failure_banner(
                body=body, attempted_sha=attempted_sha, run_url=run_url))
            logging.info("Updated sticky comment banner after failed run")
            return
    pr.create_issue_comment(_output.format_failure_notice(
        attempted_sha=attempted_sha, run_url=run_url))
    logging.info("Posted failure notice (no prior review)")


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

        # Upsert the main analysis comment: one comment per PR.
        formatted_comment, _ = build_review_comment(
            main_comment, pr_details, repo_name, config)
        upsert_analysis_comment(pr, formatted_comment)

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
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the exact comment without posting")
    args = parser.parse_args()

    # Setup environment and get PR details
    github_token, client = setup_environment()
    pr_details = get_pr_details(github_token, args.repo, args.pr)

    # Analyze PR with Gemini
    logging.info("Analyzing PR with Gemini...")
    try:
        analysis = analyze_with_gemini(client, pr_details)
    except BotInternalError as e:
        notice = e.public_message
        if notice is None:
            link = workflow_run_url()
            notice = f"Bot error during {e.step}."
            if link:
                notice += f" See the workflow run: {link}."
            notice += " No review was posted."
        if args.dry_run:
            print(notice)
            sys.exit(1)
        try:
            g = Github(github_token)
            repo = g.get_repo(args.repo)
            pr = repo.get_pull(pr_details["number"])
            record_failure(pr, pr_details.get("head_sha", ""),
                           workflow_run_url())
        except Exception as post_e:
            logging.error(f"Failed to post error notice: {post_e}")
        sys.exit(1)
    logging.debug("Analysis response received")
    logging.debug(analysis)

    # Post the comment with formatted analysis
    logging.info("Posting analysis to PR...")
    if args.dry_run:
        config = load_config()
        body, _ = build_review_comment(
            analysis, pr_details, args.repo, config)
        print(body)
        return
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
