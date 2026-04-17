#!/usr/bin/env python3
"""
apex-agent-config.py
Model, budget, and prompt configuration for the Apex Claude Agent.
All prompts and limits live here — no prompt strings in apex-agent.py.
"""
from datetime import datetime, timezone

# ── Model ─────────────────────────────────────────────────────────────────────
# Tiered: Sonnet for high-stakes reasoning, Haiku for routine/informational modes.
# Signal-review decides real trades — needs best reasoning. Everything else is
# threshold checks, summaries, or retrospective analysis — Haiku handles fine.
AGENT_MODEL = 'claude-sonnet-4-6'   # default fallback (Anthropic)

MODEL_BY_MODE = {
    'signal-review':      'claude-sonnet-4-6',    # HIGH stakes — approves/vetoes real trades
    'interactive':        'claude-sonnet-4-6',    # open-ended, needs best reasoning
    'exit-optimizer':     'claude-haiku-4-5-20251001',  # LOW — protective threshold checks
    'morning-analysis':   'claude-haiku-4-5-20251001',  # MEDIUM — synthesis, informational only
    'eod-review':         'claude-haiku-4-5-20251001',  # MEDIUM — retrospective, no live decisions
    'intraday-check':     'claude-haiku-4-5-20251001',  # LOW — simple health check
    'post-trade-autopsy': 'claude-haiku-4-5-20251001',  # MEDIUM — learning, no live decisions
}

# Gemini equivalents — used when apex_llm_client provider == 'gemini'
GEMINI_MODEL_BY_MODE = {
    'signal-review':      'gemini-2.5-pro',    # HIGH stakes — best Gemini model
    'interactive':        'gemini-2.5-pro',    # open-ended
    'exit-optimizer':     'gemini-2.5-flash',  # LOW — fast/cheap
    'morning-analysis':   'gemini-2.5-flash',  # MEDIUM
    'eod-review':         'gemini-2.5-flash',  # MEDIUM
    'intraday-check':     'gemini-2.5-flash',  # LOW
    'post-trade-autopsy': 'gemini-2.5-flash',  # MEDIUM
}

# ── Budget limits per mode (USD) ──────────────────────────────────────────────
# Opus 4.6: $15/Mtok input, $75/Mtok output
BUDGET_BY_MODE = {
    'morning-analysis':   0.05,   # Haiku — cheap
    'eod-review':         0.05,   # Haiku — cheap
    'intraday-check':     0.03,   # Haiku — very simple
    'signal-review':      0.15,   # Sonnet — needs reasoning budget
    'exit-optimizer':     0.03,   # Haiku — threshold checks
    'post-trade-autopsy': 0.05,   # Haiku — retrospective
    'interactive':        0.50,   # Sonnet — open-ended
}

# Max tool calls per run (circuit breaker against runaway loops)
MAX_TOOL_CALLS_BY_MODE = {
    'morning-analysis':   25,
    'eod-review':         20,
    'intraday-check':     8,
    'signal-review':      12,
    'exit-optimizer':     8,
    'post-trade-autopsy': 10,
    'interactive':        30,
}

# ── Signal review state file ──────────────────────────────────────────────────
# Written by signal-review mode. Read by apex-autopilot.py.
AGENT_REVIEW_FILE = '/home/ubuntu/.picoclaw/logs/apex-agent-review.json'

# How long autopilot waits for agent review before proceeding (minutes)
AGENT_REVIEW_WINDOW_MINS = 15

# Max tokens for a single API call
MAX_TOKENS = 4096

# ── Feature flag file ─────────────────────────────────────────────────────────
AGENT_FLAG_FILE = '/home/ubuntu/.picoclaw/logs/apex-agent-enabled.json'

# ── Confirmation state file ───────────────────────────────────────────────────
AGENT_CONFIRM_FILE = '/home/ubuntu/.picoclaw/logs/apex-agent-pending-confirm.json'

# ── Reasoning log ─────────────────────────────────────────────────────────────
AGENT_REASONING_LOG = '/home/ubuntu/.picoclaw/logs/apex-agent-reasoning.jsonl'
AGENT_LOG_FILE      = '/home/ubuntu/.picoclaw/logs/apex-agent.log'

# ── System prompt ─────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """\
You are the Apex Trading Agent — an autonomous trading assistant managing a real-money \
portfolio on Trading 212. You use tools to analyse markets, manage risk, and protect profits.

TODAY: {today}
MARKET STATUS: {market_status}

OPERATING MODE: AUTONOMOUS
You act on your own judgement for protective actions. You learn from outcomes and improve.

AUTONOMOUS ACTIONS — you can do these WITHOUT human confirmation:
1. tighten_stop — move a stop closer to current price (never farther). Always safe.
2. write_agent_review — PROCEED or VETO a pending signal based on your analysis.
3. log_agent_action — record your decisions for learning.
4. send_telegram — notify the operator of what you did and why.

HUMAN CONFIRMATION STILL REQUIRED FOR:
1. Opening new positions (execute-trade tools other than tighten_stop).
2. Any action that INCREASES risk exposure.
The rule: if it reduces risk, do it. If it increases risk, ask first.

SAFETY RULES — NEVER VIOLATE:
1. Always read current state before acting. Never assume positions, regime, or prices.
2. If a tool returns an error, log it and continue. Do NOT retry the same tool more than once.
3. The circuit breaker OVERRIDES all decisions. If SUSPEND or CRITICAL, protect only — no new entries.
4. tighten_stop enforces one-directional movement. Trust the tool's safety check.

LEARNING:
After every autonomous action, call log_agent_action with your confidence level. \
Your post-trade-autopsy mode will compare your actions to actual outcomes and update \
your track record. Use the track record to calibrate your confidence over time: \
if your high-confidence vetoes are often correct, trust them more. If your stop \
tightening causes premature exits, be more conservative.

{track_record}

STYLE:
- Be concise in reasoning. Prefer tool calls over lengthy prose.
- When sending Telegram messages, write for a human trader: short, direct, no jargon.
- Use pound sterling (£) for all position values.
- After autonomous actions, ALWAYS send a Telegram explaining what you did.
"""

TRACK_RECORD_FILE = '/home/ubuntu/.picoclaw/logs/apex-agent-track-record.json'
CONTEXT_FILE      = '/home/ubuntu/.picoclaw/logs/apex-context.md'
CONTEXT_MAX_AGE_MINS = 60  # if stale, rebuild before using


def _load_context_md() -> str:
    """Return apex-context.md content, rebuilding it if missing or stale.

    The agent reads this once at session start instead of calling 10+ query
    tools — see https://every.to/guides/agent-native on the context.md pattern.
    """
    import os, subprocess, time
    try:
        age = time.time() - os.path.getmtime(CONTEXT_FILE)
    except OSError:
        age = 1e9
    if age > CONTEXT_MAX_AGE_MINS * 60:
        try:
            subprocess.run(
                ['/home/ubuntu/bin/python3',
                 '/home/ubuntu/.picoclaw/scripts/apex-context-builder.py'],
                timeout=30, capture_output=True, check=False,
            )
        except Exception:
            pass
    try:
        with open(CONTEXT_FILE) as f:
            return f.read()
    except OSError:
        return '(context file unavailable — call query tools for current state)'


def system_prompt(market_status: str = 'unknown') -> str:
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')

    # Load track record if available
    track_record_str = ''
    try:
        import json as _json
        with open(TRACK_RECORD_FILE) as f:
            tr = _json.load(f)
        if tr.get('total_actions', 0) > 0:
            lines = ['YOUR TRACK RECORD (learn from this):']
            lines.append(f"  Total actions: {tr['total_actions']}")
            for atype, stats in tr.get('by_type', {}).items():
                lines.append(f"  {atype}: {stats.get('count', 0)} actions, "
                             f"accuracy={stats.get('accuracy', 'unknown')}")
            if tr.get('pnl_impact'):
                lines.append(f"  Estimated PNL impact: {tr['pnl_impact']}")
            if tr.get('lesson'):
                lines.append(f"  Key lesson: {tr['lesson']}")
            track_record_str = '\n'.join(lines)
    except (FileNotFoundError, Exception):
        track_record_str = 'TRACK RECORD: No data yet. Build it by acting and logging actions.'

    base = SYSTEM_PROMPT.format(
        today=today,
        market_status=market_status,
        track_record=track_record_str,
    )

    # Append the live context doc so the agent boots with full state awareness.
    return base + '\n\n---\n# LIVE SESSION CONTEXT\n\n' + _load_context_md()


# ── Task prompts by mode ──────────────────────────────────────────────────────
TASK_PROMPTS = {

    'morning-analysis': """\
Perform the morning analysis routine. Work through these steps in order:

1. Run the morning-health chain (staleness-check, data-integrity, reconcile).
   If any step fails, note it but continue.

2. Run the morning-regime chain (market-direction, breadth, regime, scaling, drawdown, circuit-breaker).
   If circuit-breaker is SUSPEND or CRITICAL, report this to Telegram and stop — do not proceed.

3. Read current positions (query-positions). For each open position, note signal type, \
   current P&L, and distance to stop.

4. Run the signal-pipeline chain (geo-news, macro-signals, sentiment, multiframe, vix-correlation, contrarian-scan).

5. Read query-signals to check if there is a pending signal or queued trade.

6. Synthesise: what is the overall picture? Are there risks to current positions? \
   Is the system ready to trade? Is there a signal worth highlighting?

7. Send a concise morning brief via Telegram (3–6 bullet points, plain English).
""",

    'eod-review': """\
Perform the end-of-day review. Work through these steps:

1. Read current positions and P&L (query-positions, query-performance).

2. Run the performance-review chain (sharpe, mae-mfe, backtest-v2, rolling-pnl, \
   opportunity-cost, learning-digest).

3. Run the learning-cycle chain (weight-optimizer, trajectory-learner, edge-proof, score-adapter).

4. Summarise: how did today go? What did the system learn? Any positions to watch tomorrow?

5. Send an EOD summary via Telegram (4–8 bullet points).
""",

    'signal-review': """\
A pending trade signal has been generated by the Apex decision engine. \
Your job is to review it thoroughly and give a verdict before the autopilot executes.

The signal details are in the CONTEXT section below. Work through these steps:

1. Read current regime and health (query-regime, query-health).
   If circuit-breaker is SUSPEND or CRITICAL, verdict is VETO regardless of signal quality.

2. Read current positions (query-positions).
   Check: sector concentration, portfolio heat, how many positions are already open.

3. CORRELATION GUARD — check for correlated exposure:
   Look at each open position's sector. If the new signal is in the same sector as an \
   existing position, flag it. If 2+ existing positions share the sector, this is a \
   concentration risk. Same-sector entries when an existing same-sector position is \
   underwater are especially dangerous. If you identify high correlation risk, \
   this alone can justify a VETO.

4. EDGE PROOF FILTER — check signal type track record:
   Use read_state_file for apex-edge-proof.json. Find this signal's type (e.g. CONTRARIAN, \
   INVERSE, TREND). If ALL of these are true:
     - n_real >= 5 (enough live trades to judge)
     - expectancy_r < 0 (negative expected value)
     - verdict is NOT_PROVEN
   Then this signal type is actively losing money. Verdict should be VETO unless \
   the score is exceptionally high (adjusted_score >= 9) or macro context is compelling.

5. Read macro and sentiment context for the signal instrument:
   Use read_state_file for apex-macro-signals.json, apex-sentiment.json, apex-geo-news.json.

6. ENTRY QUALITY — assess intraday timing:
   Use read_state_file for apex-intraday-momentum.json (if available). Also consider the \
   signal's entry price vs current market. Ask:
   - Is the entry price above VWAP? (premium entry = worse fill, lower edge)
   - Has the stock already moved significantly since the signal was generated?
   - If the signal is stale (>30 min old) and price has moved >1% from signal entry, \
     the risk/reward has shifted — lean toward VETO or NEUTRAL.

7. Consider the signal against all context. Ask yourself:
   - Does the macro environment support this trade type?
   - Is the sector healthy or deteriorating?
   - Are there any red flags (earnings, news, geo, correlation with existing positions)?
   - Does the risk/reward make sense at current market conditions?

8. Send a Telegram message with your analysis (max 10 lines):
   - Signal name, type, score
   - Edge proof status for this signal type (WR%, n trades, expectancy)
   - Correlation check result
   - Your key observations (2–3 bullets)
   - Your verdict and default action

9. Write your verdict to the review file using the write_agent_review tool.
   You MUST commit to one of:
   - PROCEED: signal looks good, autopilot should execute as planned
   - VETO: you see a meaningful risk the rules missed, autopilot should NOT execute
   Do NOT use NEUTRAL. You are autonomous — make the call. If in doubt, VETO \
   (protecting capital is the default). Then call log_agent_action to record your decision.

SIGNAL CONTEXT:
{signal_context}
""",

    'intraday-check': """\
Quick intraday health check:

1. Read regime (query-regime) and health (query-health).
2. Check circuit breaker status. If triggered, send an alert via Telegram immediately.
3. Read positions (query-positions). Flag any position where current price is within 2% of stop.
4. Check for a pending signal (query-signals). If one exists and is stale (>45 min), note it.

If everything looks normal, send a brief "all clear" Telegram. \
If anything needs attention, describe it clearly.
""",

    'exit-optimizer': """\
AUTONOMOUS intraday exit timing. Your goal: protect unrealised gains by tightening stops \
when momentum fades. You ACT directly — you do not just advise.

1. Run intraday_momentum to get fresh analysis for all open positions.

2. Read current positions (query-positions) for entry prices, stops, targets, current P&L.

2b. Read MAE/MFE calibration (read_state_file for apex-mae-mfe-calibration.json). \
    Key fields to use: \
    - aggregate.mfe.optimal_exit_r — the R-multiple at which winners empirically peak (median MFE). \
      If a position's current unrealised gain is >= 90% of this value AND momentum is fading, \
      tighten the stop aggressively to lock in gains. Don't wait for T1 — empirically we never reach it. \
    - aggregate.mfe.reached_t1_pct — if this is below 20%, T1 is unreachable and stops should be \
      tightened near the current price when momentum fades, not held loosely hoping for T1. \
    - aggregate.mae.stop_efficiency — if WIDE, stops are too loose and we're giving back too much.

3. Read MAE/MFE calibration (read_state_file for apex-mae-mfe-calibration.json) to understand \
   what normal MFE capture looks like for this system.

4. For each position with verdict FADING or EXHAUSTED from the momentum analysis, evaluate:
   a. R-multiple > 1.0 AND reversing from session high by >2.0%? → TIGHTEN stop to session \
      low or recent support level. This is "giving back gains" — act now.
   b. Position past T1 AND momentum fading? → TIGHTEN stop to just below T1 to lock in gains.
   c. RSI > 75 on 15m AND volume fading? → TIGHTEN stop to VWAP level as protection.
   d. Position open > 5 days with < 0.3R and momentum FADING? → TIGHTEN to near break-even.
   e. R-multiple < 0.5 and no reversal yet? → DON'T tighten. Too early, let it develop.

5. For positions with verdict STRONG or NEUTRAL: no action needed. Skip them.

6. EXECUTE: For each position that needs tightening:
   a. Calculate the new stop price (must be HIGHER than current stop).
   b. Call tighten_stop with the ticker, new price, and reason.
   c. Call log_agent_action to record what you did.
   d. The tool will refuse if the new stop isn't tighter — trust the safety check.

7. NOTIFY: After all actions, send ONE Telegram message summarising what you did:
   - Which stops were tightened, old → new, and why
   - Which positions were reviewed and found healthy
   - If no action taken on any position, do NOT send a Telegram.

8. DO NOT call trailing-stop or broker-watchdog — those are full recalculations. \
   Use tighten_stop for surgical, one-directional adjustments only.
""",

    'post-trade-autopsy': """\
Perform a post-trade analysis on recent closes AND diagnose any currently open positions \
showing negative P&L. Extract lessons and update your track record for learning.

1. Read the outcomes file (read_state_file for apex-outcomes.json). Look at trades closed \
   in the last 7 days.

1b. Read current open positions (query_positions). Identify any with negative unrealised P&L. \
    For each loser: how far from entry? Is it approaching the stop? What has changed \
    since entry (regime, sector, instrument-specific)? Should the stop be held or is \
    early exit warranted?

2. Read your own actions (read_state_file for apex-agent-actions.json). Did you take any \
   actions on this position? (tightened stop, vetoed/approved the original signal, etc.)

3. For each recently closed trade, analyse:
   a. Entry quality: was the entry near VWAP or at a premium? Was RSI at entry already extended?
   b. MFE captured: what fraction of the peak gain (MFE) was actually realised? \
      If MFE was 10% but P&L was 1%, that is a 90% MFE leakage problem.
   c. MAE exposure: how deep did the position go against before recovering (or stopping out)?
   d. Hold time efficiency: was the capital deployed for an appropriate duration vs the R achieved?
   e. Exit trigger: was it a stop hit, target hit, manual exit, or reconciliation? \
      Was the exit optimal given what happened after?
   f. AGENT IMPACT: if you tightened the stop, did it help (saved losses) or hurt (premature exit)?

4. Read the edge proof data (read_state_file for apex-edge-proof.json). \
   How does this trade's outcome compare to the signal type's track record?

5. Read the learned weights (read_state_file for apex-learned-weights.json). \
   Which scoring layers fired on this signal? Were the high-weight layers correct?

6. Synthesise ONE key lesson from this trade. Examples:
   - "CONTRARIAN entries in the energy sector have now lost 3 of 4 — sector is in structural decline"
   - "MFE leakage on this trade was 85% — trailing stop was 4% wide when ATR was only 1.5%"
   - "Agent tightened stop from $0.62 to $0.648 — position hit new stop, saving £2.40 vs original"
   - "Agent tightened stop prematurely — stock recovered to T1 after agent's exit"

7. Log your self-assessment using log_agent_action with action_type relevant to the lesson. \
   Include your updated confidence calibration.

8. Send a Telegram message with the autopsy (max 10 lines):
   - Trade summary: ticker, type, P&L (£), R-multiple, days held
   - MFE captured vs peak
   - Agent actions taken on this trade (if any) and their impact
   - The key lesson
   - Updated edge proof status for this signal type

Do NOT propose any new trades. This mode is purely analytical and for learning.
""",

}


def task_prompt(mode: str) -> str:
    return TASK_PROMPTS.get(mode, f'Mode "{mode}" has no task prompt defined.')
