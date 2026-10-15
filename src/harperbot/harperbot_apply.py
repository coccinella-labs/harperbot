# SPDX-License-Identifier: MIT
# Copyright (c) 2026 coccinella-labs

"""
HarperBot Apply Module
Handles user-approved application of code suggestions, committed as HarperBot.
"""

import logging

# Flask imported conditionally for webhook mode
flask_available = False
try:
    from flask import jsonify

    flask_available = True
except ImportError:
    pass

# Assuming these are imported from harperbot
# from harperbot.harperbot import setup_environment_webhook, get_pr_details_webhook, analyze_with_gemini, parse_code_suggestions, apply_suggestions_to_pr


def handle_apply_comment(installation_id, repo_name, pr_number):
    """
    Handle /apply comment on PR: re-analyze and apply suggestions as HarperBot.
    """
    if not flask_available:
        logging.error("Flask not available for webhook mode")
        return {"error": "Flask not installed"}, 500

    try:
        from harperbot.harperbot import (
            analyze_with_gemini,
            apply_suggestions_to_pr,
            get_pr_details_webhook,
            parse_code_suggestions,
            setup_environment_webhook,
        )

        g = setup_environment_webhook(installation_id)
        pr_details = get_pr_details_webhook(g, repo_name, pr_number)
        analysis = analyze_with_gemini(pr_details)
        suggestions = parse_code_suggestions(analysis)
        if suggestions:
            repo = g.get_repo(repo_name)
            pr = repo.get_pull(pr_number)
            apply_suggestions_to_pr(repo, pr, suggestions)
            # Post confirmation comment
            pr.create_issue_comment("Applied code suggestions from HarperBot analysis.")
            logging.info(f"Applied suggestions to PR #{pr_number} via /apply")
        else:
            # No suggestions
            pr.create_issue_comment("No code suggestions found to apply.")
        return jsonify({"status": "applied"})
    except Exception as e:
        logging.error(f"Error handling apply comment: {str(e)}")
        return jsonify({"error": "Apply failed"}), 500
