#!/usr/bin/env python3
"""
apex-agent-tools.py
Converts apex-tool-manifest.json + apex-tool-chains.json into Claude API tool definitions.

All 66 Apex tools become tool_use definitions.
Plus 4 meta-tools: run_chain, read_state_file, send_telegram, request_confirmation.

Tool names use underscores (API requirement); dispatcher converts back to hyphens.
"""
import json
import os
import re

SCRIPTS_DIR = '/home/ubuntu/.picoclaw/scripts'
LOGS_DIR    = '/home/ubuntu/.picoclaw/logs'
MANIFEST    = os.path.join(SCRIPTS_DIR, 'apex-tool-manifest.json')
CHAINS_FILE = os.path.join(SCRIPTS_DIR, 'apex-tool-chains.json')

# Pricing per million tokens by model
MODEL_PRICING = {
    'claude-sonnet-4-6':          (3.0,  15.0),
    'claude-opus-4-6':            (15.0, 75.0),
    'claude-haiku-4-5-20251001':  (0.80, 4.0),
    # Gemini 2.5 pricing (approx, per Google's published rates)
    'gemini-2.5-pro':             (1.25, 10.0),
    'gemini-2.5-flash':           (0.15,  0.60),
}
# Default (Sonnet)
PRICE_INPUT_PER_MTOK  = 3.0
PRICE_OUTPUT_PER_MTOK = 15.0


def _to_tool_name(apex_name: str) -> str:
    """Convert hyphenated Apex tool name to underscore Claude tool name."""
    return apex_name.replace('-', '_')


def _to_apex_name(tool_name: str) -> str:
    """Convert underscore Claude tool name back to hyphenated Apex tool name."""
    return tool_name.replace('_', '-')


def load_manifest() -> dict:
    with open(MANIFEST) as f:
        return json.load(f)


def load_chains() -> dict:
    with open(CHAINS_FILE) as f:
        return json.load(f)


def generate_tool_definitions() -> list[dict]:
    """
    Returns the `tools` parameter for client.messages.create().
    Each entry is a dict with keys: name, description, input_schema.
    """
    manifest = load_manifest()
    chains   = load_chains()
    tools    = []

    # ── 66 Apex tools from manifest ──────────────────────────────────────────
    # close-position is defined below as a meta-tool with full input schema — skip the
    # minimal manifest entry to avoid a duplicate that breaks Gemini's tool declarations.
    _META_TOOL_NAMES = {'close-position'}
    for t in manifest.get('tools', []):
        apex_name  = t['name']
        if apex_name in _META_TOOL_NAMES:
            continue
        safety     = t['safety']
        desc       = t['description']
        outputs    = t.get('outputs', [])
        tags       = t.get('tags', [])

        full_desc = f"[{safety}] {desc}"
        if outputs:
            full_desc += f" Writes: {', '.join(outputs)}."
        if tags:
            full_desc += f" Tags: {', '.join(tags)}."

        # execute-trade tools require explicit confirm flag
        if safety == 'execute-trade':
            input_schema = {
                'type': 'object',
                'properties': {
                    'confirm': {
                        'type': 'boolean',
                        'description': (
                            'Must be true. Only set after human confirmation has been '
                            'received via request_confirmation().'
                        ),
                    }
                },
                'required': ['confirm'],
            }
        else:
            input_schema = {'type': 'object', 'properties': {}}

        tools.append({
            'name':         _to_tool_name(apex_name),
            'description':  full_desc,
            'input_schema': input_schema,
        })

    # ── Meta-tool: run_chain ─────────────────────────────────────────────────
    chain_names = list(chains.get('chains', {}).keys())
    chain_descriptions = {
        name: chains['chains'][name].get('description', '')
        for name in chain_names
    }
    chain_desc_str = '\n'.join(
        f"  - {n}: {chain_descriptions[n]}" for n in chain_names
    )
    tools.append({
        'name': 'run_chain',
        'description': (
            'Run a named multi-step tool chain. Chains execute tools sequentially '
            'and return a summary of all steps.\n\nAvailable chains:\n' + chain_desc_str
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'chain_name': {
                    'type': 'string',
                    'enum': chain_names,
                    'description': 'Name of the chain to run.',
                }
            },
            'required': ['chain_name'],
        },
    })

    # ── Meta-tool: read_state_file ───────────────────────────────────────────
    tools.append({
        'name': 'read_state_file',
        'description': (
            'Read an Apex state JSON file from the logs directory. '
            'Use the "fields" param to extract only the fields you need — saves tokens. '
            'Example: filename="apex-edge-proof.json", fields="by_signal_type.CONTRARIAN" '
            'returns only the CONTRARIAN section instead of the full file.'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'filename': {
                    'type': 'string',
                    'description': 'Filename (not full path) of the JSON file in the logs directory.',
                },
                'fields': {
                    'type': 'string',
                    'description': (
                        'Optional dot-separated field path(s) to extract. '
                        'Comma-separated for multiple fields. '
                        'Examples: "status", "by_signal_type.CONTRARIAN", '
                        '"trades,timestamp". Omit to return the full file.'
                    ),
                },
            },
            'required': ['filename'],
        },
    })

    # ── Meta-tool: send_telegram ─────────────────────────────────────────────
    tools.append({
        'name': 'send_telegram',
        'description': (
            'Send a message to the operator via Telegram. '
            'Use for: status updates, analysis summaries, alerts, and trade proposals '
            'that require human review. Keep messages concise and actionable.'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'message': {
                    'type': 'string',
                    'description': 'Message text to send. Markdown supported.',
                }
            },
            'required': ['message'],
        },
    })

    # ── Meta-tool: request_confirmation ─────────────────────────────────────
    tools.append({
        'name': 'request_confirmation',
        'description': (
            'Request human confirmation for a consequential action (e.g., a trade). '
            'Writes a pending confirmation record and waits for the operator to reply '
            'AGENT CONFIRM or AGENT REJECT via Telegram. '
            'Returns {"confirmed": true/false, "timed_out": true/false}. '
            'ALWAYS call send_telegram first to describe what you are requesting approval for, '
            'then call this tool to wait for the response.'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'action_description': {
                    'type': 'string',
                    'description': 'Plain English description of what will happen if confirmed.',
                },
                'timeout_seconds': {
                    'type': 'integer',
                    'description': 'How long to wait for a response. Default 300 (5 minutes).',
                    'default': 300,
                },
            },
            'required': ['action_description'],
        },
    })

    # ── Meta-tool: write_agent_review ────────────────────────────────────────
    tools.append({
        'name': 'write_agent_review',
        'description': (
            'Write your signal review verdict to the review state file. '
            'This is read by apex-autopilot.py to decide whether to execute the trade. '
            'Call this AFTER send_telegram with your analysis. '
            'Verdicts: '
            'PROCEED = signal looks good, autopilot should execute; '
            'VETO = meaningful risk identified, autopilot should NOT execute; '
            'NEUTRAL = uncertain, wait for human input (operator must send AGENT CONFIRM or AGENT REJECT).'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'verdict': {
                    'type': 'string',
                    'enum': ['PROCEED', 'VETO', 'NEUTRAL'],
                    'description': 'Your recommendation for this signal.',
                },
                'reasoning_summary': {
                    'type': 'string',
                    'description': 'One sentence explaining the key reason for your verdict.',
                },
                'signal_timestamp': {
                    'type': 'string',
                    'description': (
                        'The generated_at or created_at timestamp of the signal being reviewed. '
                        'Copy exactly from the signal data — autopilot uses this to match review to signal.'
                    ),
                },
                'confidence': {
                    'type': 'number',
                    'description': 'Your confidence in the verdict, 0.0 to 1.0.',
                },
            },
            'required': ['verdict', 'reasoning_summary', 'signal_timestamp'],
        },
    })

    # ── Meta-tool: tighten_stop ─────────────────────────────────────────────
    tools.append({
        'name': 'tighten_stop',
        'description': (
            'Autonomously tighten a stop order for an open position. '
            'ONE-DIRECTIONAL: can only move the stop CLOSER to current price (higher for longs), '
            'never farther away. This is safe for autonomous use — it can only reduce risk. '
            'The tool will refuse to loosen a stop or set it above current price. '
            'Cancels the existing stop order and places a new tighter one via T212 API. '
            'Logs the action to apex-agent-actions.json for learning.'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                't212_ticker': {
                    'type': 'string',
                    'description': 'T212 ticker of the position (e.g. NFE_US_EQ, ULVRl_EQ).',
                },
                'new_stop': {
                    'type': 'number',
                    'description': (
                        'New stop price in pounds/dollars (NOT pence). '
                        'Must be higher than the current stop. GBX conversion handled automatically.'
                    ),
                },
                'reason': {
                    'type': 'string',
                    'description': 'Brief reason for tightening (e.g. "MFE leakage: reversed 8.5% from high").',
                },
            },
            'required': ['t212_ticker', 'new_stop', 'reason'],
        },
    })

    # ── Meta-tool: close_position ──────────────────────────────────────────
    tools.append({
        'name': 'close_position',
        'description': (
            'Market-close an open T212 position. This is EXECUTE-TRADE and is gated: '
            'requires a prior request_confirmation AND confirm=true in the call. '
            'Cancels any working stop order, places a market sell for the full quantity, '
            'and marks the position as "closing" in positions.json. Will refuse if the '
            'venue is currently closed (T212 rejects market orders outside hours) or if '
            'the circuit breaker is SUSPEND/CRITICAL. Use this for: exits on deteriorating '
            'fundamentals, stale signals that have lost their edge, or regime-driven '
            'de-risking. Do NOT use for routine profit-taking — that is what tighten_stop '
            'is for.'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                't212_ticker': {
                    'type': 'string',
                    'description': 'T212 ticker of the position (e.g. NFE_US_EQ, ULVRl_EQ).',
                },
                'reason': {
                    'type': 'string',
                    'description': 'Why this close is happening (goes into agent-actions log).',
                },
                'confirm': {
                    'type': 'boolean',
                    'description': 'Must be true. Additionally, a human confirmation via '
                                   'request_confirmation must have been received this session.',
                },
            },
            'required': ['t212_ticker', 'reason', 'confirm'],
        },
    })

    # ── Meta-tool: log_agent_action ─────────────────────────────────────────
    tools.append({
        'name': 'log_agent_action',
        'description': (
            'Log an autonomous agent action for learning and track record calculation. '
            'Call this after every autonomous decision (veto, tighten stop, etc). '
            'The post-trade-autopsy mode reads these to calculate agent PNL impact.'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'action_type': {
                    'type': 'string',
                    'enum': ['signal_vetoed', 'signal_approved', 'stop_tightened',
                             'exit_recommended', 'no_action'],
                    'description': 'Type of action taken.',
                },
                'ticker': {
                    'type': 'string',
                    'description': 'Ticker symbol involved.',
                },
                'details': {
                    'type': 'string',
                    'description': 'Brief description of what was done and why.',
                },
                'confidence': {
                    'type': 'number',
                    'description': 'Agent confidence in this decision, 0.0 to 1.0.',
                },
            },
            'required': ['action_type', 'ticker', 'details'],
        },
    })

    return tools


# ── Mode-scoped tool filtering ──────────────────────────────────────────────
# Each mode only gets the tools it actually needs. This saves ~5k tokens/run
# for focused modes like exit-optimizer. Full list used for interactive/morning.

TOOLS_BY_MODE = {
    'exit-optimizer': {
        'intraday_momentum', 'query_positions', 'read_state_file',
        'send_telegram', 'tighten_stop', 'log_agent_action',
    },
    'signal-review': {
        'query_regime', 'query_health', 'query_positions', 'query_signals',
        'read_state_file', 'send_telegram', 'write_agent_review',
        'log_agent_action', 'intraday_momentum', 'vwap_gate',
    },
    'intraday-check': {
        'query_regime', 'query_health', 'query_positions', 'query_signals',
        'read_state_file', 'send_telegram',
    },
    'post-trade-autopsy': {
        'query_positions', 'query_performance', 'read_state_file',
        'send_telegram', 'log_agent_action',
    },
    # morning-analysis, eod-review, interactive: use ALL tools
}


def generate_tool_definitions_for_mode(mode: str = None) -> list[dict]:
    """
    Returns tool definitions scoped to a specific mode.
    Focused modes get only the tools they need (saves ~5k tokens).
    Broad modes (morning, eod, interactive) get everything.
    """
    all_tools = generate_tool_definitions()
    if not mode or mode not in TOOLS_BY_MODE:
        return all_tools
    allowed = TOOLS_BY_MODE[mode]
    return [t for t in all_tools if t['name'] in allowed]


def estimate_cost_usd(input_tok: int, output_tok: int, model: str = None) -> float:
    """Estimate USD cost for given token counts and model."""
    inp_price, out_price = MODEL_PRICING.get(
        model or '', (PRICE_INPUT_PER_MTOK, PRICE_OUTPUT_PER_MTOK)
    )
    return (
        input_tok  / 1_000_000 * inp_price +
        output_tok / 1_000_000 * out_price
    )


if __name__ == '__main__':
    tools = generate_tool_definitions()
    print(f"Generated {len(tools)} tool definitions")
    for t in tools:
        safety_tag = ''
        m = re.match(r'^\[([^\]]+)\]', t['description'])
        if m:
            safety_tag = f" [{m.group(1)}]"
        print(f"  {t['name']}{safety_tag}")
