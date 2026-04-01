# Security Policy

## Supported Versions
This project is currently maintained on the `main` branch.

## Reporting a Vulnerability
If you find a security issue, please report it privately and do not open a public issue with exploit details.

Recommended process:
- Email the maintainer directly or use a private disclosure channel.
- Include reproduction steps, impact, and affected files.
- Allow time for a fix before public disclosure.

## Security Notes for This Project
- This app reads a local OAuth token from `~/.claude/.credentials.json`.
- Credentials are not stored in this repository.
- The token is passed from Python to Node via stdin (not command-line args).
- Temporary icon files are created with secure temp-file APIs and removed on exit.

## Safe Publishing Checklist
Before publishing:
- Ensure `.claude/` is ignored.
- Ensure `__pycache__/` is ignored.
- Ensure `~/.config/claude-status/config.json` is not committed.
- Do not commit debug logs that include account/usage metadata.
- Run `git status --short` and review every tracked change.

## Scope
This tool is a desktop usage indicator and is not intended for handling highly sensitive secrets beyond local OAuth token usage required for API access.
