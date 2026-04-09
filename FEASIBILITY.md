# Feasibility Analysis: Claude Usage Tracking Against Weekly/Session Limits

**Bead:** cl-wv2
**Date:** 2026-04-09
**Author:** polecat/rust

---

## 1. Prior Art

### Claude-Specific Trackers (Active Ecosystem)

| Project | Stars | Approach | Notes |
|---------|-------|----------|-------|
| **claude-devtools** | ~3000 | Visual DevTools for Claude Code | Session logs, tool calls, token usage, context window |
| **tokscale** | ~1700 | CLI multi-tool tracker | Tracks Claude Code, Codex, Gemini CLI; global leaderboard |
| **phuryn/claude-usage** | ~700 | Local dashboard | Token usage, costs, session history; Pro/Max progress bar |
| **claude-code-usage-bar** | ~190 | Statusline plugin | Real-time token usage, remaining budget, burn rate |
| **splitrail** | ~150 | Cross-platform tracker (Rust) | Claude Code, Gemini CLI, Codex CLI |
| **ClaudeUsageTracker** | ~110 | macOS menu bar app | Cost calculations |
| **ocodista/claude-usage** | ~22 | Local file parser | Per-project/session costs |
| **CCDash** | ~60 | Unified dashboard | Claude Code + claude.ai + API data |

**Key pattern:** Nearly all work by **parsing Claude Code's local JSONL session files** in `~/.claude/projects/`. No dominant tool has emerged yet. Most are macOS-centric. Few are Rust-based.

### General AI API Trackers

- **LiteLLM** (~42K stars) — dominant open-source proxy/gateway, 100+ providers, cost tracking
- **Helicone** — commercial observability platform (proxy-based)
- **LangSmith** — LangChain's observability platform
- **Portkey** — AI gateway with cost tracking
- **tokentap** (~780 stars) — intercepts LLM API traffic, terminal dashboard

---

## 2. Data Sources (Feasibility)

### 2a. Claude Code Local Session Files (CONFIRMED - Best Source)

**Location:** `~/.claude/projects/<project-dir>/<session-id>.jsonl`

Each line is a JSON object. Assistant messages include:

```json
{
  "type": "assistant",
  "message": {
    "usage": {
      "input_tokens": 10,
      "cache_creation_input_tokens": 8050,
      "cache_read_input_tokens": 21576,
      "output_tokens": 38,
      "service_tier": "standard"
    },
    "model": "claude-haiku-4-5-20251001"
  }
}
```

**Confirmed fields per message:**
- `input_tokens`, `output_tokens` — billable tokens
- `cache_creation_input_tokens`, `cache_read_input_tokens` — prompt caching
- `model` — which model was used
- `timestamp` — when the message was sent
- `sessionId` — session identifier

**Session metadata** in `~/.claude/sessions/<pid>.json`:
- `pid`, `sessionId`, `cwd`, `startedAt`, `kind`, `entrypoint`

**Strengths:** Already on disk, no API calls needed, per-message granularity, includes model info.
**Weaknesses:** Undocumented format (could change between Claude Code versions), no cost field (must calculate from token counts + model pricing), no weekly limit info.

### 2b. Anthropic API Response Headers (CONFIRMED)

Every API response includes rate limit headers:

| Header | Description |
|--------|-------------|
| `anthropic-ratelimit-requests-limit` | Max requests/min |
| `anthropic-ratelimit-requests-remaining` | Requests left in window |
| `anthropic-ratelimit-tokens-limit` | Max tokens/min |
| `anthropic-ratelimit-tokens-remaining` | Tokens left in window |
| `anthropic-ratelimit-input-tokens-limit` | Input tokens/min limit |
| `anthropic-ratelimit-output-tokens-limit` | Output tokens/min limit |

**Strengths:** Real-time rate limit awareness, per-minute granularity.
**Weaknesses:** Only available if intercepting API calls (proxy approach); per-minute only, no weekly/monthly limits exposed; not accessible from local file parsing.

### 2c. Anthropic API Response Body (CONFIRMED)

Every Messages API response includes a `usage` object:

```json
{
  "usage": {
    "input_tokens": 42,
    "output_tokens": 128,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 0
  }
}
```

**This is exactly what Claude Code writes to the JSONL files**, so parsing local files gives us the same data without needing to intercept API calls.

### 2d. Anthropic Admin API (CONFIRMED - Org-Level)

Endpoint: `GET /v1/organizations/{org_id}/usage`

- Requires Admin API key (separate from regular API keys)
- Aggregated usage by workspace, API key, model, time period
- Created in Console under Settings > Admin API Keys

**Strengths:** Historical aggregate data, official API.
**Weaknesses:** Requires admin access, org-level (not per-session), may not cover Claude Code Pro/Max subscription usage (only API usage).

### 2e. Weekly/Monthly Limit Info (NOT AVAILABLE PROGRAMMATICALLY)

**This is the gap.** Anthropic does not expose weekly or monthly spending limits via any public API. The limits shown in the Anthropic Console dashboard are:
- Rate limits (per-minute) — available via headers
- Spending limits (monthly) — set in Console, not queryable via API
- Pro/Max subscription limits — not exposed anywhere programmatically

**Workaround options:**
1. User manually configures their limit in the tool's config
2. Scrape the Console dashboard (fragile, likely TOS violation)
3. Infer from rate limit tier (tiers correspond to spend levels, but imprecise)

---

## 3. Architecture Options

### Option A: Local Log Parser (Recommended)

```
~/.claude/projects/**/*.jsonl  →  [Parser]  →  [Aggregator]  →  [Display]
```

- Parse JSONL session files from disk
- Aggregate token counts by session, project, day, week
- Calculate costs using known model pricing
- Display via CLI (terminal dashboard) or statusline

**Pros:** No API keys needed, works offline, no proxy complexity, covers all Claude Code usage.
**Cons:** Undocumented format, no real rate limit data, must hardcode pricing.

### Option B: API Proxy

```
Claude Code  →  [Proxy]  →  Anthropic API
                   ↓
              [Usage DB]  →  [Dashboard]
```

- Intercept API calls, record headers + usage
- Get real-time rate limit info from headers

**Pros:** Real-time rate limits, works for any API client (not just Claude Code).
**Cons:** Complex setup (must configure proxy), latency overhead, Claude Code may not support custom base URLs easily.

### Option C: Hybrid (Local Parser + Admin API)

- Parse local files for per-session/per-message data
- Query Admin API for org-level aggregates and reconciliation
- User configures weekly limit manually

**Pros:** Best of both worlds, can cross-reference.
**Cons:** Requires admin API key, more complex.

### Option D: Claude Code Hook/Plugin

- Use Claude Code hooks (settings.json) to capture usage data on each message
- Hook runs after each assistant response, extracts usage from the JSONL

**Pros:** Event-driven (no polling), clean integration.
**Cons:** Hooks have limited access to message internals; would still need to parse JSONL.

---

## 4. Recommendations

### Primary Approach: Local Log Parser (Option A)

**Rationale:**
1. The data is already there — Claude Code writes per-message token counts to JSONL files
2. No API keys or special access needed
3. This is exactly what the most successful existing tools do (tokscale, claude-usage, splitrail)
4. Rust is ideal for fast file parsing and real-time file watching (via `notify` crate)

### Implementation Plan

1. **Core parser** — Read `~/.claude/projects/**/*.jsonl`, extract usage data per message
2. **Aggregator** — Sum by session, project, day, week, model
3. **Cost calculator** — Apply model pricing (hardcoded, updatable via config)
4. **Limit tracker** — User configures weekly limit in config; tool shows progress bar
5. **Display** — Terminal dashboard (TUI via `ratatui`) or simple CLI output
6. **File watcher** — `notify` crate for real-time updates as sessions progress

### Weekly Limit Tracking

Since weekly limits aren't available programmatically, the tool should:
1. Let users set their own weekly budget in a config file (e.g., `~/.claudemeter/config.toml`)
2. Track cumulative spend across the week (Mon-Sun)
3. Show percentage used, burn rate, projected depletion

### Differentiation Opportunities

The existing ecosystem is crowded but fragmented. A Rust tool could differentiate by:
- **Cross-platform** (most existing tools are macOS-only)
- **Fast** (Rust for parsing large JSONL files)
- **Real-time** (file watching, not polling)
- **Configurable limits** (weekly, daily, per-project budgets)
- **Multi-tool** (Claude Code + other AI CLIs if they use similar patterns)

---

## 5. Risks and Open Questions

| Risk | Mitigation |
|------|-----------|
| JSONL format changes between Claude Code versions | Version detection, graceful degradation, pin to known schemas |
| No official weekly limit API | User-configured limits; monitor Anthropic API changelog |
| Cost calculation accuracy (pricing changes) | Config-driven pricing table, easy to update |
| Large JSONL files (heavy users) | Incremental parsing, seek to last-read position |
| Pro/Max subscription tracking (different from API) | May need separate approach; out of scope for v1 |

### Open Questions
1. Does Claude Code's `costUSD` field exist in newer versions? (Not found in current files — may be a planned feature)
2. Will Anthropic add a spending/limit API? (Worth monitoring)
3. Should we support claude.ai web usage too? (Different data source, likely requires browser extension)
