<p align="center">
  <img src="https://raw.githubusercontent.com/basebin/harperbot/main/.github/assets/thumbnail.png" alt="harperbot" width="100%">
</p>

# HarperBot

Automated code review tool using Gemini or Cerebras AI for GitHub pull requests.

## Setup

1. Clone: `git clone https://github.com/harpertoken/harperbot.git`

2. Install: `pip install -e .`

3. Configure `harperbot/config.yaml`:
   - Set `provider: gemini` or `provider: cerebras`

4. Set environment variables:
   - `GEMINI_API_KEY` (for Gemini)
   - `CEREBRAS_API_KEY` (for Cerebras)
   - `GITHUB_TOKEN`

5. Run CLI: `python harperbot/harperbot.py --repo owner/repo --pr 123`

## Development

- Tests: `python -m pytest test/test_harperbot.py`
- Linting: `pre-commit run --all-files`
- Build: `python -m build`

## Contributing

1. Fork and clone the repo.
2. Install dependencies: `pip install -e .`
3. Install pre-commit: `pre-commit install`
4. Set API keys.
5. Create branch, code, test, commit conventionally, push, PR.

## Integration

- Add to repo: `bin/setup-harperbot`
- Webhook: Use `api/webhook.py` with Gunicorn
- CI/CD: Copy `.github/workflows/harperbot.yml`

## Configuration

Customize `harperbot/config.yaml`:
- **Provider**: `gemini` or `cerebras`
- **Model**: `gemini-2.5-flash` (Gemini) or `gpt-oss-120b` (Cerebras)
- **Focus**: `all`, `security`, `performance`, `quality`
- **Features**: Safety (Gemini), authoring, custom functions (Gemini)

Extracted from Friday Gemini AI project. See `docs/README.md` for details.
