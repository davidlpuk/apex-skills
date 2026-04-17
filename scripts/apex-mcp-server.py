#!/usr/bin/env python3
"""
apex-mcp-server.py
MCP server that exposes all 66 Apex tools to Claude Code sessions.

Runs via stdio (standard MCP transport). Claude Code discovers tools via
tools/list and calls them via tools/call — all dispatched through the
existing apex-tool-runner.py safety layer.

execute-trade tools are blocked in MCP mode. They require the Telegram
confirmation flow (AGENT CONFIRM) which only the standalone agent supports.

Usage (configured in .mcp.json):
    /home/ubuntu/bin/python3 /home/ubuntu/.picoclaw/scripts/apex-mcp-server.py
"""
import json
import os
import subprocess
import sys

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')

import mcp.server.stdio
import mcp.types as types
from mcp.server import Server

from apex_agent_tools import generate_tool_definitions, _to_apex_name
from apex_utils import safe_read

SCRIPTS_DIR = '/home/ubuntu/.picoclaw/scripts'
LOGS_DIR    = '/home/ubuntu/.picoclaw/logs'
PYTHON      = '/home/ubuntu/bin/python3'
TOOL_RUNNER = os.path.join(SCRIPTS_DIR, 'apex-tool-runner.py')

# execute-trade tools are blocked in MCP — require Telegram confirmation flow
BLOCKED_SAFETY_LEVELS = {'execute-trade'}

server = Server('apex-trading')


def _get_manifest() -> dict:
    path = os.path.join(SCRIPTS_DIR, 'apex-tool-manifest.json')
    with open(path) as f:
        return json.load(f)


def _get_safety(apex_name: str) -> str:
    manifest = _get_manifest()
    for t in manifest.get('tools', []):
        if t['name'] == apex_name:
            return t['safety']
    return 'read'


def _run_apex_tool(apex_name: str) -> str:
    """Execute via apex-tool-runner.py. Returns result as JSON string."""
    cmd = [PYTHON, TOOL_RUNNER, '--run', apex_name]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=320,
            cwd=SCRIPTS_DIR,
        )
        if not proc.stdout.strip():
            return json.dumps({'status': 'error', 'error': proc.stderr[:500] or 'empty output'})
        # Validate it's JSON before returning
        parsed = json.loads(proc.stdout)
        return json.dumps(parsed, indent=2)
    except subprocess.TimeoutExpired:
        return json.dumps({'status': 'error', 'error': 'timed out after 320s'})
    except json.JSONDecodeError:
        return proc.stdout[:4000]
    except Exception as e:
        return json.dumps({'status': 'error', 'error': str(e)})


def _run_chain(chain_name: str) -> str:
    cmd = [PYTHON, TOOL_RUNNER, '--chain', chain_name]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,
            cwd=SCRIPTS_DIR,
        )
        if not proc.stdout.strip():
            return json.dumps({'status': 'error', 'error': proc.stderr[:500] or 'empty output'})
        parsed = json.loads(proc.stdout)
        return json.dumps(parsed, indent=2)
    except subprocess.TimeoutExpired:
        return json.dumps({'status': 'error', 'error': 'timed out after 600s'})
    except json.JSONDecodeError:
        return proc.stdout[:4000]
    except Exception as e:
        return json.dumps({'status': 'error', 'error': str(e)})


def _read_state_file(filename: str) -> str:
    basename = os.path.basename(filename)
    if not basename.startswith('apex-') or not basename.endswith('.json'):
        return json.dumps({'error': f'Disallowed: {basename}. Must match apex-*.json'})
    data = safe_read(os.path.join(LOGS_DIR, basename), None)
    if data is None:
        return json.dumps({'error': f'{basename} not found or unreadable'})
    return json.dumps(data, indent=2)


# ── Tool registration ──────────────────────────────────────────────────────────

@server.list_tools()
async def list_tools() -> list[types.Tool]:
    tool_defs = generate_tool_definitions()
    tools = []
    for td in tool_defs:
        apex_name = _to_apex_name(td['name'])
        safety = _get_safety(apex_name) if apex_name not in (
            'run-chain', 'read-state-file', 'send-telegram', 'request-confirmation'
        ) else 'read'

        # Mark execute-trade tools as blocked in description
        desc = td['description']
        if safety in BLOCKED_SAFETY_LEVELS:
            desc = f"[BLOCKED in MCP — requires Telegram AGENT CONFIRM] {desc}"

        tools.append(types.Tool(
            name=td['name'],
            description=desc,
            inputSchema=td['input_schema'],
        ))
    return tools


# ── Tool execution ─────────────────────────────────────────────────────────────

@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    apex_name = _to_apex_name(name)

    # ── Meta-tools ────────────────────────────────────────────────────────────
    if name == 'run_chain':
        chain = arguments.get('chain_name', '')
        result = _run_chain(chain)
        return [types.TextContent(type='text', text=result)]

    if name == 'read_state_file':
        filename = arguments.get('filename', '')
        result = _read_state_file(filename)
        return [types.TextContent(type='text', text=result)]

    if name in ('send_telegram', 'request_confirmation'):
        return [types.TextContent(type='text', text=json.dumps({
            'status': 'blocked',
            'reason': (
                'send_telegram and request_confirmation are only available in the '
                'standalone agent (apex-agent.py). In Claude Code, communicate '
                'with the operator directly via the conversation.'
            ),
        }))]

    # ── Safety gate: block execute-trade ─────────────────────────────────────
    safety = _get_safety(apex_name)
    if safety in BLOCKED_SAFETY_LEVELS:
        return [types.TextContent(type='text', text=json.dumps({
            'status': 'blocked',
            'reason': (
                f'{apex_name} is an [execute-trade] tool. It cannot be called from '
                'a Claude Code MCP session. To execute: send AGENT ON via Telegram, '
                'then AGENT CONFIRM when the agent proposes the action.'
            ),
        }))]

    result = _run_apex_tool(apex_name)
    return [types.TextContent(type='text', text=result)]


# ── Entry point ───────────────────────────────────────────────────────────────

async def main():
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == '__main__':
    import asyncio
    asyncio.run(main())
