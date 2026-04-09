#!/usr/bin/env python3
"""claudemeter — Track Claude token usage against 5-hour and 7-day rate limit windows.

Parses Claude Code's local JSONL session files and aggregates token usage
by model and token type within Anthropic's unified rate limit windows.
"""

import json
import os
import glob
import sys
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path


# ── Rate limit windows ──────────────────────────────────────────────────────
FIVE_HOUR_SECONDS = 18_000   # 5h rolling window
SEVEN_DAY_SECONDS = 604_800  # 7d rolling window

# ── Token type weights for unified rate limit ───────────────────────────────
# These are the "effective token" weights Anthropic uses to calculate
# unified rate limit utilization. Source: Claude Code binary analysis.
#
# The unified rate limit converts all token types to a single "effective
# token" count using these multipliers:
TOKEN_WEIGHTS = {
    "input_tokens": 1,
    "output_tokens": 5,               # Output tokens count 5x
    "cache_creation_input_tokens": 1,  # Same as input
    "cache_read_input_tokens": 0.1,    # Heavily discounted
}

# ── Model display names ─────────────────────────────────────────────────────
MODEL_DISPLAY = {
    "claude-opus-4-6": "Opus 4.6",
    "claude-sonnet-4-6": "Sonnet 4.6",
    "claude-sonnet-4-5-20250514": "Sonnet 4.5",
    "claude-haiku-4-5-20251001": "Haiku 4.5",
    "<synthetic>": "(synthetic)",
}


def model_short(model: str) -> str:
    if model in MODEL_DISPLAY:
        return MODEL_DISPLAY[model]
    # Strip common prefixes
    for prefix in ("claude-", ):
        if model.startswith(prefix):
            model = model[len(prefix):]
    return model


# ── Data structures ─────────────────────────────────────────────────────────

@dataclass
class TokenBucket:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    message_count: int = 0

    def add(self, usage: dict):
        self.input_tokens += usage.get("input_tokens", 0) or 0
        self.output_tokens += usage.get("output_tokens", 0) or 0
        self.cache_creation_input_tokens += usage.get("cache_creation_input_tokens", 0) or 0
        self.cache_read_input_tokens += usage.get("cache_read_input_tokens", 0) or 0
        self.message_count += 1

    @property
    def total_raw(self) -> int:
        return (self.input_tokens + self.output_tokens +
                self.cache_creation_input_tokens + self.cache_read_input_tokens)

    @property
    def effective_tokens(self) -> float:
        """Weighted token count matching Anthropic's unified rate limit formula."""
        return (
            self.input_tokens * TOKEN_WEIGHTS["input_tokens"]
            + self.output_tokens * TOKEN_WEIGHTS["output_tokens"]
            + self.cache_creation_input_tokens * TOKEN_WEIGHTS["cache_creation_input_tokens"]
            + self.cache_read_input_tokens * TOKEN_WEIGHTS["cache_read_input_tokens"]
        )


@dataclass
class WindowData:
    """Aggregated data for a single time window (5h or 7d)."""
    by_model: dict = field(default_factory=lambda: defaultdict(TokenBucket))
    total: TokenBucket = field(default_factory=TokenBucket)
    oldest_message: datetime | None = None
    newest_message: datetime | None = None
    sessions_seen: set = field(default_factory=set)


# ── JSONL parsing ───────────────────────────────────────────────────────────

def parse_jsonl_files(base_dir: str, cutoff_7d: datetime, cutoff_5h: datetime) -> tuple[WindowData, WindowData]:
    """Parse all JSONL session files and aggregate into 5h and 7d windows."""
    window_5h = WindowData()
    window_7d = WindowData()

    jsonl_pattern = os.path.join(base_dir, "*", "*.jsonl")
    files = glob.glob(jsonl_pattern)

    skipped_old = 0
    parse_errors = 0
    files_read = 0

    for fpath in files:
        # Skip files not modified in the 7d window (fast pre-filter)
        try:
            mtime = os.path.getmtime(fpath)
            mtime_dt = datetime.fromtimestamp(mtime, tz=timezone.utc)
            if mtime_dt < cutoff_7d:
                skipped_old += 1
                continue
        except OSError:
            continue

        # Extract session ID from filename
        session_id = Path(fpath).stem

        files_read += 1
        try:
            with open(fpath, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        parse_errors += 1
                        continue

                    if obj.get("type") != "assistant":
                        continue

                    ts_str = obj.get("timestamp")
                    if not ts_str:
                        continue

                    try:
                        msg_time = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    except ValueError:
                        continue

                    msg = obj.get("message", {})
                    usage = msg.get("usage", {})
                    model = msg.get("model", "unknown")

                    if not usage:
                        continue

                    # Skip synthetic/zero-usage messages
                    if model == "<synthetic>":
                        continue

                    # 7-day window
                    if msg_time >= cutoff_7d:
                        window_7d.by_model[model].add(usage)
                        window_7d.total.add(usage)
                        window_7d.sessions_seen.add(session_id)
                        if window_7d.oldest_message is None or msg_time < window_7d.oldest_message:
                            window_7d.oldest_message = msg_time
                        if window_7d.newest_message is None or msg_time > window_7d.newest_message:
                            window_7d.newest_message = msg_time

                    # 5-hour window
                    if msg_time >= cutoff_5h:
                        window_5h.by_model[model].add(usage)
                        window_5h.total.add(usage)
                        window_5h.sessions_seen.add(session_id)
                        if window_5h.oldest_message is None or msg_time < window_5h.oldest_message:
                            window_5h.oldest_message = msg_time
                        if window_5h.newest_message is None or msg_time > window_5h.newest_message:
                            window_5h.newest_message = msg_time

        except (OSError, IOError):
            continue

    return window_5h, window_7d


# ── Display formatting ──────────────────────────────────────────────────────

def fmt_num(n: int | float) -> str:
    """Format a number with comma separators."""
    if isinstance(n, float):
        if n >= 1_000_000:
            return f"{n / 1_000_000:.1f}M"
        if n >= 1_000:
            return f"{n / 1_000:.1f}K"
        return f"{n:.0f}"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def bar(fraction: float, width: int = 30) -> str:
    """Render a simple progress bar."""
    filled = int(fraction * width)
    filled = min(filled, width)
    return f"[{'#' * filled}{'.' * (width - filled)}]"


def print_window(name: str, window: WindowData, window_seconds: int, now: datetime):
    """Print summary for one time window."""
    cutoff = now - timedelta(seconds=window_seconds)

    print(f"\n{'=' * 70}")
    print(f"  {name} WINDOW")
    if window_seconds == FIVE_HOUR_SECONDS:
        print(f"  Since: {cutoff.strftime('%Y-%m-%d %H:%M')} UTC  (rolling 5h)")
    else:
        print(f"  Since: {cutoff.strftime('%Y-%m-%d %H:%M')} UTC  (rolling 7d)")
    print(f"{'=' * 70}")

    if window.total.message_count == 0:
        print("  (no messages in this window)")
        return

    print(f"  Messages: {window.total.message_count:,}   "
          f"Sessions: {len(window.sessions_seen)}")
    if window.oldest_message and window.newest_message:
        span = window.newest_message - window.oldest_message
        hours = span.total_seconds() / 3600
        print(f"  Span: {window.oldest_message.strftime('%H:%M')} — "
              f"{window.newest_message.strftime('%H:%M')} UTC ({hours:.1f}h)")

    # ── Totals ──
    t = window.total
    eff = t.effective_tokens
    print(f"\n  Token Totals:")
    print(f"    Input (fresh):      {fmt_num(t.input_tokens):>10}")
    print(f"    Output:             {fmt_num(t.output_tokens):>10}  (x5 weight)")
    print(f"    Cache creation:     {fmt_num(t.cache_creation_input_tokens):>10}")
    print(f"    Cache read:         {fmt_num(t.cache_read_input_tokens):>10}  (x0.1 weight)")
    print(f"    ────────────────────────────")
    print(f"    Raw total:          {fmt_num(t.total_raw):>10}")
    print(f"    Effective (weighted): {fmt_num(eff):>8}")

    # ── Breakdown by type (% of effective) ──
    if eff > 0:
        pct_input = (t.input_tokens * TOKEN_WEIGHTS["input_tokens"]) / eff * 100
        pct_output = (t.output_tokens * TOKEN_WEIGHTS["output_tokens"]) / eff * 100
        pct_cache_create = (t.cache_creation_input_tokens * TOKEN_WEIGHTS["cache_creation_input_tokens"]) / eff * 100
        pct_cache_read = (t.cache_read_input_tokens * TOKEN_WEIGHTS["cache_read_input_tokens"]) / eff * 100

        print(f"\n  Budget Impact (% of effective tokens):")
        print(f"    Output (x5):        {bar(pct_output / 100, 25)} {pct_output:5.1f}%")
        print(f"    Input (x1):         {bar(pct_input / 100, 25)} {pct_input:5.1f}%")
        print(f"    Cache create (x1):  {bar(pct_cache_create / 100, 25)} {pct_cache_create:5.1f}%")
        print(f"    Cache read (x0.1):  {bar(pct_cache_read / 100, 25)} {pct_cache_read:5.1f}%")

    # ── Breakdown by model ──
    print(f"\n  By Model:")
    print(f"    {'Model':<20} {'Messages':>8} {'Output':>10} {'Effective':>12} {'Share':>7}")
    print(f"    {'─' * 20} {'─' * 8} {'─' * 10} {'─' * 12} {'─' * 7}")

    models_sorted = sorted(
        window.by_model.items(),
        key=lambda x: x[1].effective_tokens,
        reverse=True,
    )

    for model, bucket in models_sorted:
        share = (bucket.effective_tokens / eff * 100) if eff > 0 else 0
        print(f"    {model_short(model):<20} {bucket.message_count:>8} "
              f"{fmt_num(bucket.output_tokens):>10} "
              f"{fmt_num(bucket.effective_tokens):>12} "
              f"{share:>5.1f}%")

    # ── Per-model token type breakdown ──
    if len(models_sorted) > 1:
        print(f"\n  Per-Model Token Breakdown:")
        for model, bucket in models_sorted:
            if bucket.effective_tokens == 0:
                continue
            m_eff = bucket.effective_tokens
            print(f"\n    {model_short(model)}  (effective: {fmt_num(m_eff)}):")
            print(f"      Input:         {fmt_num(bucket.input_tokens):>10}")
            print(f"      Output:        {fmt_num(bucket.output_tokens):>10}  "
                  f"({bucket.output_tokens * TOKEN_WEIGHTS['output_tokens'] / m_eff * 100:.0f}% of budget)")
            print(f"      Cache create:  {fmt_num(bucket.cache_creation_input_tokens):>10}")
            print(f"      Cache read:    {fmt_num(bucket.cache_read_input_tokens):>10}")


def print_rate_limit_research():
    """Print findings on rate limit header availability and hooks."""
    print(f"\n{'=' * 70}")
    print("  RATE LIMIT HEADER RESEARCH")
    print(f"{'=' * 70}")
    print("""
  Data Source: Anthropic unified rate limit headers
  ─────────────────────────────────────────────────
  Every Claude API response includes these headers:
    anthropic-ratelimit-unified-<type>-utilization  (0.0-1.0)
    anthropic-ratelimit-unified-<type>-reset         (unix timestamp)
    anthropic-ratelimit-unified-status               (allowed/warning/rejected)

  Rate limit types:
    five_hour  — 5h rolling window (18,000 seconds)
    seven_day  — 7d rolling window (604,800 seconds)

  Status: NOT PERSISTED
  ─────────────────────
  These headers are read in-memory by Claude Code but NEVER written to
  the JSONL session files. They are consumed for the status bar warning
  display and then discarded.

  Claude Code Hook Investigation:
  ───────────────────────────────
  Available hook events: PreToolUse, PostToolUse, Notification,
  UserPromptSubmit, Stop

  None of these hooks expose the HTTP response headers or the parsed
  rate limit state. The rate limit data flows:
    API response → Claude Code JS runtime → status bar display
  It never touches the filesystem or the hook system.

  Possible future approaches:
  1. MITM proxy to capture response headers (complex setup)
  2. Feature request to Anthropic: persist utilization in JSONL
  3. Feature request: expose rate limit state in hook context
  4. Parse Claude Code's internal state (process memory — fragile)

  Conclusion: For now, we can only ESTIMATE utilization from token
  counts using the known weight formula. We cannot read actual
  utilization percentages without intercepting HTTP responses.
""")


def print_token_weight_findings():
    """Print findings on which token types count towards which limits."""
    print(f"\n{'=' * 70}")
    print("  TOKEN WEIGHT FINDINGS")
    print(f"{'=' * 70}")
    print("""
  How Anthropic calculates unified rate limit utilization:
  ─────────────────────────────────────────────────────────
  All token types are converted to "effective tokens" using weights:

    Token Type                    Weight    Notes
    ─────────────────────────     ──────    ─────
    input_tokens                  x1        Fresh input (not cached)
    output_tokens                 x5        HEAVIEST — dominates budget
    cache_creation_input_tokens   x1        First-time cache write
    cache_read_input_tokens       x0.1      Heavily discounted (90% off)

  Key insight: OUTPUT TOKENS are the primary budget driver.
  A single output token costs 5x an input token against your rate limit.

  Example: 1,000 output tokens = 5,000 effective tokens
           1,000 input tokens  = 1,000 effective tokens
           1,000 cache reads   =   100 effective tokens

  Warning thresholds (from Claude Code binary):
  ──────────────────────────────────────────────
  5-hour window:
    - Warn at 90% utilization when 72% of window has elapsed

  7-day window (graduated warnings):
    - Warn at 75% utilization when 60% of window elapsed
    - Warn at 50% utilization when 35% of window elapsed
    - Warn at 25% utilization when 15% of window elapsed

  Both windows:
    five_hour  — 18,000 second rolling window (5 hours)
    seven_day  — 604,800 second rolling window (7 days)
""")


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    base_dir = os.path.expanduser("~/.claude/projects")

    if not os.path.isdir(base_dir):
        print(f"Error: Claude Code projects directory not found: {base_dir}", file=sys.stderr)
        sys.exit(1)

    now = datetime.now(timezone.utc)
    cutoff_5h = now - timedelta(seconds=FIVE_HOUR_SECONDS)
    cutoff_7d = now - timedelta(seconds=SEVEN_DAY_SECONDS)

    print(f"claudemeter — Claude Token Usage Tracker")
    print(f"Scanning: {base_dir}")
    print(f"Time: {now.strftime('%Y-%m-%d %H:%M:%S')} UTC")

    window_5h, window_7d = parse_jsonl_files(base_dir, cutoff_7d, cutoff_5h)

    print_window("5-HOUR", window_5h, FIVE_HOUR_SECONDS, now)
    print_window("7-DAY", window_7d, SEVEN_DAY_SECONDS, now)

    # Show research findings
    if "--research" in sys.argv:
        print_rate_limit_research()
        print_token_weight_findings()

    # Burn rate analysis
    if window_7d.total.message_count > 0 and window_7d.oldest_message and window_7d.newest_message:
        span = (window_7d.newest_message - window_7d.oldest_message).total_seconds()
        if span > 0:
            eff = window_7d.total.effective_tokens
            rate_per_hour = eff / (span / 3600)
            print(f"\n{'=' * 70}")
            print(f"  BURN RATE (7-day window)")
            print(f"{'=' * 70}")
            print(f"  Effective tokens/hour: {fmt_num(rate_per_hour)}")
            print(f"  Effective tokens/day:  {fmt_num(rate_per_hour * 24)}")

    print(f"\nTip: Run with --research to see rate limit header findings")


if __name__ == "__main__":
    main()
