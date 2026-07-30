# Fixes and pending improvements

---

## Pending

### 3. Agent capability registry — planner should see what each agent can do

The planner defaults to `owner=human` for anything involving AWS, GitHub, or deployment
tools because it has no visibility into what credentials and CLIs are available to agents.

**Design:** Add `.orchestra/capabilities.yaml` to each managed repo describing available
tools and credentials per agent identity. The root agent reads this during
`_discover_context()` and injects it into the planner system prompt. Add a `devops-agent`
owner type for infra/CI work. Three feedback loops keep it current:

1. **Auto-probe at request time** — root agent runs `which aws`, `gh auth status`, etc.
   and writes results to the capabilities file before planning.
2. **Agent write-back** — after successfully using a tool, agents append to the file via
   `write_artifact`, building capability knowledge from experience.
3. **Human seeding** — `orchctl teach devops-agent "..."` for explicit capability facts.

**Files to change:** `permissions/agent-tools.yaml` (platform-level defaults),
`plan_utils.py` (inject capability context into planner prompt), `dispatcher.py` (handle
`devops-agent` owner), `cli/main.py` (add `devops-agent` to valid owner list),
`CLAUDE.md` (document new owner type).

**Managed repo:** Add `.orchestra/capabilities.yaml` describing what tools/credentials
are available for that specific project (not global — each repo has its own context).

---

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
