# SPDX-License-Identifier: MIT
# Copyright (c) 2026 coccinella-labs

"""Review output: structured findings, sanitization, comment formatting.

Pure functions only. Nothing here touches the network, so all of it is
unit-testable without secrets, quota, or API access. The single impure
boundary is the caller, which fetches data and posts the built strings.
"""

from dataclasses import dataclass
import json
import logging
import re

SEVERITIES = ("critical", "high", "medium", "low", "info")

# Extended sticky marker. shown/attempted may differ after a failed run on
# top of a good review; status is ok or failed.
MARKER_RE = re.compile(
    r"<!-- harperbot:summary shown:(?P<shown>[0-9a-f]{4,64})"
    r" attempted:(?P<attempted>[0-9a-f]{4,64})"
    r" status:(?P<status>ok|failed) -->"
)

LEGACY_MARKERS = (
    "<!-- harperbot-analysis -->",
    "<summary>HarperBot Analysis</summary>",
)


def find_legacy(body):
    """True for pre-protocol bot comments (old marker or summary shell)."""
    text = body or ""
    return any(m in text for m in LEGACY_MARKERS)


@dataclass
class Finding:
    """One actionable review finding. Validated, never trusted raw."""

    severity: str
    category: str
    path: str
    line: int | None
    message: str
    suggestion: str | None = None

    def validate(self):
        """Raise ValueError describing the first problem found."""
        if self.severity not in SEVERITIES:
            raise ValueError(f"bad severity: {self.severity!r}")
        if not self.category or not isinstance(self.category, str):
            raise ValueError("category must be a non-empty string")
        if not self.path or not isinstance(self.path, str):
            raise ValueError("path must be a non-empty string")
        if self.line is not None and (
            not isinstance(self.line, int)
            or isinstance(self.line, bool)
            or self.line < 1
        ):
            raise ValueError(f"line must be a positive int or null, got {self.line!r}")
        if not self.message or not isinstance(self.message, str):
            raise ValueError("message must be a non-empty string")
        if self.suggestion is not None and not isinstance(self.suggestion, str):
            raise ValueError("suggestion must be a string or null")
        return self


def parse_findings(text):
    """Extract (summary, findings, ok) from model output.

    Contract: a ```json fenced block holding an object with a "findings"
    list; "summary" optional. Anything else (no block, bad JSON, wrong
    shape, invalid entry) returns ok=False, and the caller posts the raw
    analysis with a fallback note instead of failing the run.
    """
    if not text:
        return None, [], False
    match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    if not match:
        return None, [], False
    try:
        payload = json.loads(match.group(1))
    except (json.JSONDecodeError, ValueError):
        return None, [], False
    if not isinstance(payload, dict) or not isinstance(
        payload.get("findings"), list
    ):
        return None, [], False
    findings = []
    try:
        for raw in payload["findings"]:
            if not isinstance(raw, dict):
                raise ValueError("finding entry must be an object")
            findings.append(
                Finding(
                    severity=raw.get("severity"),
                    category=raw.get("category"),
                    path=raw.get("path"),
                    line=raw.get("line"),
                    message=raw.get("message"),
                    suggestion=raw.get("suggestion"),
                ).validate()
            )
    except (ValueError, TypeError, AttributeError) as e:
        logging.debug(f"Findings validation failed: {e}")
        return None, [], False
    summary = payload.get("summary")
    if summary is not None and not isinstance(summary, str):
        return None, [], False
    return summary, findings, True


def _neutralize_mentions(text):
    """Neutralize @mentions with a zero-width space (invisible in render).

    org/team form (with slash) is always a mention and never valid code,
    so it is always neutralized. Bare @word is neutralized in prose;
    suggestion code is left alone so copied code still works.
    """
    text = re.sub(r"@([\w-]+/[\w-]+)", "@\u200b\\1", text)
    return text


def sanitize_model_text(text, prose=True):
    """Make model-written text safe to render in a comment.

    Escapes comment openers (marker forgery), strips stray HTML, drops
    remote images, neutralizes mentions. Returns "" for empty input.
    """
    if not text:
        return ""
    text = text.replace("<!--", "&lt;!--")
    text = re.sub(r"!\[[^\]]*\]\(https?://[^)]+\)", "", text)
    if prose:
        text = re.sub(r"@([\w-]+)", "@\u200b\\1", text)
    else:
        text = _neutralize_mentions(text)
    text = re.sub(r"</?[a-zA-Z][^>]*>", "", text)
    return text


CHECK_RUN_NAME = "HarperBot review"

# At most three actions per check run. Re-running re-analyzes the same
# commit through the normal pipeline; GitHub restricts button use to
# accounts with write access, so no separate author check is needed.
CHECK_RUN_ACTIONS = [
    {"label": "Re-run analysis",
     "description": "Run HarperBot analysis again on this commit",
     "identifier": "rerun-analysis"},
]

# Annotation levels: notice and warning only. Never error/failure: the
# Files-changed tab must not show red marks the conclusion won't back up.
def _annotation_level(severity):
    return "warning" if severity in ("critical", "high") else "notice"


def parse_diff_added_lines(diff):
    """Map each file to its added-line ranges: {path: [(start, end)]}.

    Only added lines can carry inline findings; everything else belongs
    in annotations or the summary.
    """
    added = {}
    path = None
    new_line = 0
    for line in (diff or "").split("\n"):
        if line.startswith("+++ b/"):
            path = line[6:]
            added.setdefault(path, [])
        elif line.startswith("@@"):
            m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
            if m and path is not None:
                new_line = int(m.group(1))
        elif path is not None and new_line:
            if line.startswith("+") and not line.startswith("+++"):
                added[path].append((new_line, new_line))
                new_line += 1
            elif line.startswith("-"):
                pass
            else:
                new_line += 1
    # Coalesce adjacent single lines into ranges.
    for path, spans in added.items():
        merged = []
        for start, end in sorted(spans):
            if merged and start <= merged[-1][1] + 1:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        added[path] = [tuple(x) for x in merged]
    return added


def split_findings_by_diff(findings, added):
    """Partition into (in_diff, out_of_diff).

    In-diff findings are covered by the review path; only out-of-diff
    findings become check annotations, so no finding is posted twice.
    """
    in_diff, out = [], []
    for f in findings:
        ranges = added.get(f.path, [])
        line = f.line or 1
        if any(s <= line <= e for s, e in ranges):
            in_diff.append(f)
        else:
            out.append(f)
    return in_diff, out


def finding_to_annotation(finding):
    """One check-annotation dict. File-level findings anchor at line 1."""
    line = finding.line or 1
    return {
        "path": finding.path,
        "start_line": line,
        "end_line": line,
        "annotation_level": _annotation_level(finding.severity),
        "message": f"[{finding.severity}/{finding.category}] {finding.message}",
    }


def chunk_annotations(annotations, size=50):
    """Split into API-sized batches (50 per request)."""
    return [annotations[i:i + size] for i in range(0, len(annotations), size)]


def check_conclusion(findings, failed):
    """success with nothing to say, neutral otherwise. Never failure."""
    if failed or findings:
        return "neutral"
    return "success"


def build_check_summary(*, commit_sha, model, findings, analysis,
                        failed=False, run_url=None):
    """Check-run summary markdown. Long tail in <details>."""
    head = [f"Reviewed `{commit_sha[:7]}` · {model or 'unknown model'} · "
            f"{counts_line(findings)}"]
    if failed:
        head.append("This run did not complete; results below are partial.")
    return ("\n".join(head) + "\n\n<details>\n<summary>Full analysis</summary>\n\n"
            + sanitize_model_text(analysis) + "\n\n</details>")


def counts_line(findings):
    """Counts by severity, e.g. '1 high · 2 medium', or 'no findings'."""
    if not findings:
        return "no findings"
    parts = []
    for severity in SEVERITIES:
        n = sum(1 for f in findings if f.severity == severity)
        if n:
            parts.append(f"{n} {severity}")
    return " · ".join(parts) if parts else "no findings"


def render_metadata_line(commit_sha, commit_url, model, findings, focus="all"):
    """Single metadata line: linked short SHA, resolved model, counts."""
    bits = [f"Reviewed [`{commit_sha[:7]}`]({commit_url})", model or "unknown model",
            counts_line(findings)]
    if focus and focus != "all":
        bits.append(f"focus: {focus}")
    return " · ".join(bits)


def render_marker(shown_sha, attempted_sha, status):
    """Extended sticky marker. Parsed back by parse_marker."""
    return (
        f"<!-- harperbot:summary shown:{shown_sha} "
        f"attempted:{attempted_sha} status:{status} -->"
    )


def parse_marker(body):
    """Parse the LAST extended marker (a forged earlier one must not win)."""
    matches = list(MARKER_RE.finditer(body or ""))
    if not matches:
        return None
    m = matches[-1]
    return {
        "shown": m.group("shown"),
        "attempted": m.group("attempted"),
        "status": m.group("status"),
    }


def format_review_comment(
    *,
    analysis,
    findings,
    commit_sha,
    commit_url,
    model,
    run_url=None,
    focus="all",
    check_url=None,
):
    """Full review comment. Analysis stays open: no <details> wrapper until
    a summary exists above it (structured summary is a later phase)."""
    safe_analysis = sanitize_model_text(analysis)
    lines = [
        "## HarperBot review",
        "",
        render_metadata_line(commit_sha, commit_url, model, findings, focus),
        "",
        safe_analysis,
        "",
    ]
    footer = "".join(
        f"[{label}]({url})"
        for label, url in (("workflow run", run_url), ("check run", check_url))
        if url)
    if footer:
        lines.append(f"<sub>{footer}</sub>")
        lines.append("")
    lines.append(
        render_marker(commit_sha, commit_sha, "ok")
    )
    return "\n".join(lines)


def format_fallback_comment(*, analysis, commit_sha, commit_url, model,
                            run_url=None, focus="all", check_url=None):
    """Parse failure: raw analysis inside <details> with an explicit note."""
    safe_analysis = sanitize_model_text(analysis)
    lines = [
        "## HarperBot review",
        "",
        render_metadata_line(commit_sha, commit_url, model, [], focus),
        "",
        "Structured findings unavailable: the model's response did not "
        "contain a valid findings block, so this is the raw analysis.",
        "",
        "<details>",
        "<summary>Full analysis</summary>",
        "",
        safe_analysis,
        "",
        "</details>",
        "",
    ]
    footer = "".join(
        f"[{label}]({url})"
        for label, url in (("workflow run", run_url), ("check run", check_url))
        if url)
    if footer:
        lines.append(f"<sub>{footer}</sub>")
        lines.append("")
    lines.append(
        render_marker(commit_sha, commit_sha, "ok")
    )
    return "\n".join(lines)


def format_failure_notice(*, attempted_sha, run_url=None):
    """Short plain notice for failed-with-no-prior-review. No badge, no
    details shell, so it cannot pass for a review."""
    lines = [
        "HarperBot could not review "
        f"`{attempted_sha[:7]}`.",
    ]
    if run_url:
        lines[0] += f" ([workflow run]({run_url}))"
    return "\n".join(lines)


def apply_failure_banner(*, body, attempted_sha, run_url=None):
    """Failed run on top of a prior review: update banner and marker only,
    leave the review body untouched."""
    banner = f"Last run failed on `{attempted_sha[:7]}`"
    if run_url:
        banner += f" ([workflow run]({run_url}))"
    banner += " — showing the previous review."
    lines = body.split("\n")
    out = []
    replaced_banner = False
    for line in lines:
        if not replaced_banner and (
            line.startswith("Reviewed [`") or line.startswith("Last run failed")
        ):
            out.append(banner)
            replaced_banner = True
        elif MARKER_RE.search(line):
            continue  # dropped; fresh marker appended below
        else:
            out.append(line)
    if not replaced_banner:
        out.insert(0, banner)
        out.insert(1, "")
    # Marker: shown stays, attempted + status update.
    prior = parse_marker(body) or {}
    shown = prior.get("shown", attempted_sha)
    out.append(render_marker(shown, attempted_sha, "failed"))
    return "\n".join(out)
