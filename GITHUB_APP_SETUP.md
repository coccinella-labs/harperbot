# GitHub App Setup

Required for PR comments. Errors with `create-github-app-token` indicate missing credentials.

## Production Setup

1. **Create GitHub App**:
   - URL: https://github.com/organizations/harpertoken/settings/apps
   - Name: `HarperBot`
   - Homepage: https://github.com/coccinella-labs/harperbot
   - Webhook: Disabled
   - Permissions:
     - Contents: Read
     - Issues: Read & Write
     - Pull Requests: Read & Write

2. **Generate** private key (.pem) and note App ID.

3. **Add Secrets** in repo Settings → Actions:
   - `APP_ID`: App ID
   - `PRIVATE_KEY`: Full .pem content
   - `GEMINI_API_KEY`: Existing
   - `CEREBRAS_API_KEY`: New

4. **Workflow** uses `actions/create-github-app-token@v2` with secrets.

## Local Testing

Use `GITHUB_TOKEN` directly (limited permissions, not for production).
