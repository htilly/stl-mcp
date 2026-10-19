# stl-watch

A Claude Code plugin that starts `scripts/stl_watch.py watch` as a background monitor in every
session. When you comment on a model in the web UI, the monitor prints a `NEW_COMMENT` line, which
wakes Claude. Claude reads the comment and answers it with `update_model(..., resolves=[...])`,
without you typing anything in the chat.

## Install (once)

    /plugin marketplace add htilly/stl-mcp
    /plugin install stl-watch@stl-mcp

Then sign in once, in a terminal: `python3 <plugin dir>/scripts/stl_watch.py login`
(or put a personal token from the 🔑 dialog in `STL_MCP_TOKEN` or the macOS keychain item
`stl-mcp-token`). Until you have signed in the monitor exits silently.

Set `STL_MCP_URL` if you do not use https://autosmith.eu.

Plugin monitors are experimental in Claude Code and only run in interactive sessions.
