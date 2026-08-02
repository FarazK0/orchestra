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

### 4. Sandbox-based mutating command detection for run_command

Before executing a command via `POST /run_command`, the gateway cannot reliably determine
whether it will mutate external state (AWS, GitHub, DNS) using static analysis alone.

**Design:** Run the command twice — first as a dry probe in a network-namespaced container
with no cloud credentials injected, then (if it completes without network errors) as the
real execution with full credentials. If the probe run exits with a credential/network
error, the command is considered mutating and subject to tier checks before the real run
proceeds.

Implementation:
1. Strip cloud credentials from the subprocess environment for the probe run.
2. Launch the probe in a Docker container with `--network none` (or a network namespace
   that blocks external traffic) and a short timeout (5–10s).
3. If the probe exits 0 → command is read-only; run it normally, no tier check.
4. If the probe exits with an auth/network error → mutating; apply the same tier gate
   as Tier 2 file writes (require `tier2_override` on the task or a human gate).
5. If the probe times out → treat as mutating (conservative).

Complement with a fast verb-lookup table for well-known CLIs (`terraform plan`,
`aws * describe*`, `kubectl get`) so the probe overhead is skipped for the common
read-only cases.

**Files to change:** `gateway/gateway/app.py` (`run_command` handler), add
`gateway/gateway/sandbox.py` for the probe logic.

**Prerequisite:** Docker must be available on the gateway host (already true for this
stack). The existing `run_command` Docker sandbox (Phase 3) only isolates filesystem
access; this extends it to classify mutating intent before granting credentials.

---

### 2. Human input question truncated at 200 chars in events and audit rows

**File:** `gateway/gateway/app.py:394,406`

`body.question[:200]` is applied when writing the event payload and audit row, so long
blocker questions are silently truncated. The full text is preserved in `task.checkpoint`
but is not visible via `orchctl show` or the events API.

**Fix:** Remove the `[:200]` slice on both lines. The question field has no DB column
length constraint — the cap was defensive but unnecessary and loses information.
