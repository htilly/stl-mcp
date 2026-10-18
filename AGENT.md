# AGENT.md

## Language
Everything in this repository must be in English: code, comments, docstrings, identifiers,
error messages, tool descriptions, file names, README and other docs, commit messages and PR text.
Do not add Swedish (or any other language) text. Replies to the user may follow the user's language.

## Branches
Every new session must create its own branch before making any changes, and never commit directly
to `main`. Branch off an up-to-date `main` with a short descriptive name (e.g. `feature/<topic>`,
`fix/<topic>`), commit the session's work there, and merge it into `main` via a pull request.

## Testing
Always create tests for all new code. Every new function, tool, endpoint or behavior change must
come with automated tests that cover it, and existing tests must be updated when behavior changes.
Run the tests before committing.
Tests live in `tests/` (pytest). Install with `pip install -r requirements.txt -r requirements-dev.txt`
and run `python -m pytest`. `test_client.py` is a manual smoke script against a running server, not part of the suite.
