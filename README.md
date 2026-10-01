<p align="center">
  <img src="https://raw.githubusercontent.com/Coccinella-Labs/harperbot/main/.github/assets/thumbnail.png" alt="harperbot" width="100%">
</p>

# HarperBot

HarperBot is an automated code review tool that uses AI to analyze GitHub pull requests. It integrates with GitHub as a native app and can operate both as a CLI tool for local testing and as a webhook-driven service for automated PR analysis on every push. HarperBot supports multiple AI providers (Gemini and Cerebras) and focuses reviews on security, performance, code quality, or all three depending on configuration.

Status: Production-ready. Extracted from the Friday Gemini AI project. Actively maintained.

## Getting Started

HarperBot requires Python 3.9+, a GitHub account with repository access, and an API key from your chosen AI provider (Google Gemini or Cerebras).

To use HarperBot as a CLI tool, clone the repository with `git clone https://github.com/coccinella-labs/harperbot.git`, install the package with `pip install -e .`, then set environment variables for your API keys (`GEMINI_API_KEY` for Gemini or `CEREBRAS_API_KEY` for Cerebras) and `GITHUB_TOKEN` for GitHub access. To analyze a pull request, run `python harperbot/harperbot.py --repo owner/repo --pr 123`. HarperBot will fetch the PR diff, send it to your configured AI model, and print the analysis to stdout.

To integrate HarperBot as a GitHub App that automatically reviews all PRs in your repositories, first create a GitHub App through your account settings and register the webhook endpoint (`api/webhook.py`) with GitHub. Then run the webhook server using Gunicorn or another WSGI server, configure the API key and GitHub App credentials, and push to your repository. GitHub will send webhook payloads on every push, HarperBot will analyze the changes, and post review comments directly to the PR. For CI/CD integration, copy the workflow file from `.github/workflows/harperbot.yml` and adjust paths and provider settings as needed.

For local development, run tests with `python -m pytest test/test_harperbot.py` and lint with `pre-commit run --all-files`. Pre-commit hooks are configured in `.pre-commit-config.yaml`; install them with `pre-commit install` to catch issues before you commit.

## Architecture

HarperBot is structured as three integration points. The CLI tool at `harperbot/harperbot.py` accepts a repository and PR number, fetches the PR diff from GitHub, sends it to the AI provider, and prints results. The API webhook at `api/webhook.py` receives GitHub push payloads, extracts commit changes, routes them to the AI provider, and posts review comments back to the PR using the GitHub API. The configuration system at `harperbot/config.yaml` defines which AI provider to use, which model, and which focus areas (security, performance, code quality) should be analyzed.

The flow depends on the integration mode. In CLI mode, HarperBot is a local tool that runs on demand. In webhook mode, HarperBot runs as a service and processes every push automatically. In GitHub Actions mode, HarperBot runs as a step in your CI/CD workflow. All modes converge on the same AI analysis pipeline: fetch the diff, send it to the provider with instructions on what to look for, parse the response, and post results back to GitHub (or print locally for CLI).

Key code anchors are `harperbot/harperbot.py` (main CLI entry point and analysis orchestration), `api/webhook.py` (GitHub webhook handler), `harperbot/config.yaml` (configuration schema), and `test/test_harperbot.py` (test suite). Provider-specific integrations for Gemini and Cerebras are embedded in the analysis flow; support for additional providers can be added by extending the provider dispatcher.

## Configuration

HarperBot reads configuration from `harperbot/config.yaml`. Set `provider` to either `gemini` or `cerebras` depending on which AI service you use. Set `model` to the specific model name for your provider; `gemini-2.5-flash` is the default for Gemini and `gpt-oss-120b` for Cerebras. The `focus` setting controls what aspects of the code HarperBot analyzes: `all` covers security, performance, and quality; `security` focuses on vulnerabilities and auth issues; `performance` looks for inefficiencies and optimization opportunities; `quality` checks for maintainability and design patterns.

Environment variables override config file settings. Set `GEMINI_API_KEY` to your Google API key, `CEREBRAS_API_KEY` to your Cerebras API key, and `GITHUB_TOKEN` to a personal access token with repo read/write access. For GitHub App integration, also set `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY`, and `WEBHOOK_SECRET`. The setup script at `bin/setup-harperbot` can automate this configuration; run `bash bin/setup-harperbot` to interactively create the config file and verify your API keys.

Provider-specific options are documented in `config.yaml`. Gemini supports safety override settings for more permissive analysis; Cerebras allows tuning of model parameters for different workload types.

## API

The webhook endpoint at `api/webhook.py` listens for GitHub push events. When GitHub sends a push webhook, HarperBot extracts the commit diff, sends it to the AI provider with your configured focus settings, and posts review comments to the PR using the GitHub API. The webhook path should be registered in your GitHub App settings and secured with the webhook secret you configure.

The CLI at `harperbot/harperbot.py` accepts command-line arguments for `--repo` (owner/repo format), `--pr` (PR number), and optional `--provider` and `--focus` overrides. The command is synchronous; HarperBot fetches the PR, analyzes it, and returns results before exiting.

## Contributing

Start by reading GITHUB_APP_SETUP.md for instructions on creating and configuring a GitHub App. Then fork the repository, clone it locally, install dependencies with `pip install -e .`, and install pre-commit hooks with `pre-commit install`.

Code standards: all new code must pass `pytest` and pre-commit linting. Commit messages should follow Conventional Commits format (e.g., `feat: add cerebras provider support` or `fix: handle webhook timeouts gracefully`). When adding a new AI provider, implement the provider interface in the analysis module and add corresponding tests to `test/test_harperbot.py`. When modifying webhook behavior, test both the webhook handler and the GitHub API interaction.

## Build and Deploy

Build the package with `python -m build`. For development, install in editable mode with `pip install -e .`. To run the webhook server locally, use `gunicorn api:app` or another WSGI server. For production deployment, configure environment variables via your deployment platform (Vercel, Heroku, Docker, etc.), point your GitHub App webhook to your deployed endpoint, and ensure the `WEBHOOK_SECRET` matches the value registered in GitHub.

For GitHub Actions integration, copy the workflow file from `.github/workflows/harperbot.yml`, configure the provider and model in the workflow step, and commit it to your repository. Actions will automatically run on each PR.

Testing covers the CLI, webhook handler, and provider integrations. Run `python -m pytest test/test_harperbot.py` to execute the test suite. Add tests for new providers and integrations before merging.

## Known Limitations

Webhook processing is synchronous; very large diffs may take several seconds to analyze. GitHub has a 6,000-character limit on PR comments, so HarperBot truncates analysis results if they exceed that limit. Gemini's safety policies may reject analysis of certain code patterns (e.g., crypto implementations); Cerebras does not have this restriction. GitHub Apps require the webhook secret to be secure; if exposed, regenerate it immediately in your GitHub App settings. Rate limits apply per provider; if you hit Gemini or Cerebras rate limits, HarperBot backs off for 60 seconds before retrying.

## Troubleshooting

If the CLI fails to connect to GitHub, verify that `GITHUB_TOKEN` is set and has repo access. If the webhook is not receiving payloads from GitHub, check that the webhook URL is reachable, that the secret matches, and that your firewall allows inbound GitHub webhooks from GitHub's IP ranges. If the AI analysis is empty or too generic, check that your API key is valid and that the model supports the code language you are analyzing.

If HarperBot fails to post review comments, verify that the `GITHUB_TOKEN` has write access to the repository and that the commit or PR still exists (it may have been deleted). If you see truncated comments on very large diffs, consider splitting the PR into smaller changes. If you see rate limit errors from your AI provider, reduce the frequency of reviews or split the workload across multiple deployments.

## Performance

Webhook requests complete in 1-3 seconds for typical PRs when using Gemini, and 2-5 seconds with Cerebras. Very large diffs (>10,000 lines) may take longer. The CLI is synchronous; a PR analysis blocks until the AI provider responds. Memory usage is minimal (under 50MB) except during analysis of very large files.

## Roadmap

Support for additional AI providers (Claude, GPT-4) is planned. Streaming analysis results instead of posting after completion is under consideration. Improved handling of large diffs through chunking or multi-stage analysis is in progress. See GitHub Issues for the full roadmap and to report bugs or request features.

## Related Documentation

GITHUB_APP_SETUP.md provides step-by-step instructions for creating and configuring a GitHub App. docs/README.md contains additional technical details on architecture and provider integrations. The project was extracted from Friday Gemini AI and maintains compatibility with that project's configuration format.

## License

MIT. See LICENSE file.

## Contact

Questions? Open an issue on GitHub or see the discussion forum in the repository.
