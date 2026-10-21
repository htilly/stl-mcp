# AGENT.md

## Language
Everything in this repository must be in English: code, comments, docstrings, identifiers,
error messages, tool descriptions, file names, README and other docs, commit messages and PR text.
Do not add Swedish (or any other language) text. Replies to the user may follow the user's language.

## Branches
Every new session must create its own branch before making any changes, and never commit directly
to `main`. Branch off an up-to-date `main` with a short descriptive name (e.g. `feature/<topic>`,
`fix/<topic>`), commit the session's work there, and merge it into `main` via a pull request.

Always commit, open a PR and merge automatically: when a task is done, commit, push, open a PR to
`main` and merge it as soon as the checks pass, without asking first. Never merge while a check is
red or still running. Merging deploys to dev (see Deploying), so dev is always up to date.
Prod is the exception: only tag a release when the user asks.

## Testing
Always create tests for all new code. Every new function, tool, endpoint or behavior change must
come with automated tests that cover it, and existing tests must be updated when behavior changes.
Run the tests before committing.
Tests live in `tests/` (pytest). Install with `pip install -r requirements.txt -r requirements-dev.txt`
and run `python -m pytest`. `test_client.py` is a manual smoke script against a running server, not part of the suite.

## Deploying (dev and prod)
The repo is trunk-based: there is no `dev` branch. `main` is the only long-lived branch.

**Deploy to dev (automatic, nothing to run):** open a pull request to `main`, wait for the `pytest`
check, merge. The merge builds the image (`docker-image.yml`), and on success `deploy-dev.yml`
deploys that exact `:main-<sha>` to dev.autosmith.eu. After finishing a task, an agent should push
its branch, open a PR to `main` and merge it once checks pass.

**Deploy to prod (explicit release):** tag a commit that is already on `main` and has run on dev:

    git tag vX.Y.Z <sha-on-main> && git push origin vX.Y.Z

`release.yml` retags dev's image as `:vX.Y.Z` and `:release` (no rebuild), deploys it to
autosmith.eu, then creates a GitHub Release with generated notes. Use `vMAJOR.MINOR.PATCH`;
look at the latest tag (`git tag --sort=-v:refname | head -1`) before choosing the next one.
Only tag when the user asks for a prod release.

**Roll back prod:** run the "Release" workflow by hand with an older version, e.g. `v1.3.2`.

Rules: never push directly to `main`; never rebuild for prod; `tests.yml` runs on PRs only, pushes
to `main` are tested by `docker-image.yml`. Workflow files live in `.github/workflows/`.
