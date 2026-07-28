# Fixes and pending improvements

---

## Pending

### 1. Add Playwright MCP to the claude-code-agent

Give `claude-code-agent` full browser automation so agents can interact with web UIs
(GitHub Actions, AWS console, CloudFront status pages) without a human gate.

**What to do:** Configure the Playwright MCP server in the claude-code-agent's Claude
config (`.mcp.json` or `~/.claude.json`):

```json
{
  "mcpServers": {
    "playwright": {
      "command": "npx",
      "args": ["@playwright/mcp@latest"]
    }
  }
}
```

Gives the agent `browser_navigate`, `browser_click`, `browser_fill`, `browser_screenshot`.
Credentials (GitHub session or `GH_TOKEN`) remain the only human gate.

---

### 2. Human input question truncated at 200 chars in events and audit rows

**File:** `gateway/gateway/app.py:394,406`

`body.question[:200]` is applied when writing the event payload and audit row, so long
blocker questions are silently truncated. The full text is preserved in `task.checkpoint`
but is not visible via `orchctl show` or the events API.

**Fix:** Remove the `[:200]` slice on both lines. The question field has no DB column
length constraint — the cap was defensive but unnecessary and loses information.
