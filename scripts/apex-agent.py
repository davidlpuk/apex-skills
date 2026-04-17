#!/usr/bin/env python3
"""
apex-agent.py
Claude Opus 4.6 agent loop for Apex trading system.

Checks the feature flag before any run. Dispatches all tool calls through
apex-tool-runner.py (the single safety-gated execution layer).

CLI:
    python3 apex-agent.py --mode interactive --prompt "What is the current regime?"
    python3 apex-agent.py --mode morning-analysis
    python3 apex-agent.py --mode eod-review
    python3 apex-agent.py --mode intraday-check
    python3 apex-agent.py --mode signal-review
    python3 apex-agent.py --mode exit-optimizer
    python3 apex-agent.py --mode post-trade-autopsy
    python3 apex-agent.py --mode morning-analysis --force-enable   # bypass flag for testing
"""
import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone, timedelta

sys.path.insert(0, '/home/ubuntu/.picoclaw/scripts')

# ── Local imports ─────────────────────────────────────────────────────────────
from apex_utils import (
    atomic_write, safe_read, send_telegram, log_warning, log_info,
    locked_read_modify_write,
)
from apex_agent_tools import generate_tool_definitions, generate_tool_definitions_for_mode, _to_apex_name
from apex_agent_config import (
    AGENT_MODEL, MODEL_BY_MODE, GEMINI_MODEL_BY_MODE, BUDGET_BY_MODE,
    MAX_TOOL_CALLS_BY_MODE, MAX_TOKENS,
    AGENT_FLAG_FILE, AGENT_CONFIRM_FILE, AGENT_REASONING_LOG, AGENT_LOG_FILE,
    AGENT_REVIEW_FILE,
    system_prompt, task_prompt,
)

SCRIPTS_DIR = '/home/ubuntu/.picoclaw/scripts'
LOGS_DIR    = '/home/ubuntu/.picoclaw/logs'
PYTHON      = '/home/ubuntu/bin/python3'
TOOL_RUNNER = os.path.join(SCRIPTS_DIR, 'apex-tool-runner.py')

# Safety levels that require --force when calling tool-runner
EXECUTE_TRADE_LEVELS = {'execute-trade'}

# ── Feature flag ──────────────────────────────────────────────────────────────

def is_agent_enabled() -> bool:
    """Return True if agent is enabled. Defaults to disabled (fail-closed)."""
    data = safe_read(AGENT_FLAG_FILE, {})
    if not isinstance(data, dict):
        return False
    return bool(data.get('enabled', False))


def set_agent_enabled(enabled: bool, changed_by: str = 'code', reason: str = '') -> None:
    atomic_write(AGENT_FLAG_FILE, {
        'enabled':    enabled,
        'changed_by': changed_by,
        'changed_at': datetime.now(timezone.utc).isoformat() + 'Z',
        'reason':     reason,
    })


# ── Tool safety manifest lookup ───────────────────────────────────────────────
_manifest_cache: dict = {}

def _get_safety(apex_name: str) -> str:
    global _manifest_cache
    if not _manifest_cache:
        try:
            with open(os.path.join(SCRIPTS_DIR, 'apex-tool-manifest.json')) as f:
                data = json.load(f)
            _manifest_cache = {t['name']: t['safety'] for t in data.get('tools', [])}
        except Exception:
            pass
    return _manifest_cache.get(apex_name, 'read')


# ── Tool execution ─────────────────────────────────────────────────────────────

def execute_apex_tool(apex_name: str, force: bool = False) -> dict:
    """Run a single tool via apex-tool-runner.py. Returns the parsed result dict."""
    cmd = [PYTHON, TOOL_RUNNER, '--run', apex_name]
    if force:
        cmd.append('--force')
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=320,
            cwd=SCRIPTS_DIR,
        )
        if not proc.stdout.strip():
            return {'status': 'error', 'tool': apex_name, 'error': proc.stderr[:500] or 'empty output'}
        return json.loads(proc.stdout)
    except subprocess.TimeoutExpired:
        return {'status': 'error', 'tool': apex_name, 'error': 'timed out after 320s'}
    except json.JSONDecodeError as e:
        return {'status': 'error', 'tool': apex_name, 'error': f'invalid JSON: {e}'}
    except Exception as e:
        return {'status': 'error', 'tool': apex_name, 'error': str(e)}


def execute_chain(chain_name: str) -> dict:
    """Run a named chain via apex-tool-runner.py --chain."""
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
            return {'status': 'error', 'chain': chain_name, 'error': proc.stderr[:500] or 'empty output'}
        return json.loads(proc.stdout)
    except subprocess.TimeoutExpired:
        return {'status': 'error', 'chain': chain_name, 'error': 'timed out after 600s'}
    except json.JSONDecodeError as e:
        return {'status': 'error', 'chain': chain_name, 'error': f'invalid JSON: {e}'}
    except Exception as e:
        return {'status': 'error', 'chain': chain_name, 'error': str(e)}


def read_state_file(filename: str, fields: str = None) -> dict:
    """Read a state JSON file, optionally extracting specific fields to save tokens."""
    basename = os.path.basename(filename)
    if not basename.startswith('apex-') or not basename.endswith('.json'):
        return {'error': f'Disallowed filename: {basename}. Must match apex-*.json'}
    path = os.path.join(LOGS_DIR, basename)
    data = safe_read(path, None)
    if data is None:
        return {'error': f'{basename} not found or unreadable'}

    if not fields:
        return data

    # Extract specific fields using dot-notation paths
    result = {}
    for field_path in fields.split(','):
        field_path = field_path.strip()
        if not field_path:
            continue
        parts = field_path.split('.')
        val = data
        for part in parts:
            if isinstance(val, dict):
                val = val.get(part)
            elif isinstance(val, list) and part.isdigit():
                idx = int(part)
                val = val[idx] if idx < len(val) else None
            else:
                val = None
                break
        result[field_path] = val

    return result


def send_telegram_tool(message: str) -> dict:
    """Send a Telegram message. Returns status dict."""
    try:
        send_telegram(message)
        return {'status': 'sent', 'length': len(message)}
    except Exception as e:
        return {'status': 'error', 'error': str(e)}


def request_confirmation(action_description: str, timeout_seconds: int = 300) -> dict:
    """
    Write pending confirmation, poll for human response.
    Returns {"confirmed": bool, "timed_out": bool}.
    """
    confirm_id = str(uuid.uuid4())[:8]
    atomic_write(AGENT_CONFIRM_FILE, {
        'confirm_id':         confirm_id,
        'action_description': action_description,
        'requested_at':       datetime.now(timezone.utc).isoformat() + 'Z',
        'confirmed':          None,  # None = pending
    })

    deadline = time.monotonic() + timeout_seconds
    poll_interval = 5

    while time.monotonic() < deadline:
        time.sleep(poll_interval)
        data = safe_read(AGENT_CONFIRM_FILE, {})
        if not isinstance(data, dict):
            continue
        # Only accept responses for this specific request
        if data.get('confirm_id') != confirm_id:
            # File was overwritten by a newer request — abandon
            return {'confirmed': False, 'timed_out': False, 'reason': 'confirm_id mismatch'}
        confirmed = data.get('confirmed')
        if confirmed is True:
            try:
                os.remove(AGENT_CONFIRM_FILE)
            except OSError:
                pass
            return {'confirmed': True, 'timed_out': False}
        if confirmed is False:
            try:
                os.remove(AGENT_CONFIRM_FILE)
            except OSError:
                pass
            return {'confirmed': False, 'timed_out': False}

    # Timed out — clean up
    try:
        os.remove(AGENT_CONFIRM_FILE)
    except OSError:
        pass
    return {'confirmed': False, 'timed_out': True}


# ── Market status helper ──────────────────────────────────────────────────────

def get_market_status() -> str:
    data = safe_read(os.path.join(LOGS_DIR, 'apex-market-calendar.json'), {})
    if not isinstance(data, dict):
        return 'unknown'
    today = data.get('today', {})
    status = today.get('status', 'unknown')
    uk_open = today.get('uk_currently_open', False)
    us_open = today.get('us_currently_open', False)
    return f"{status} | UK={'OPEN' if uk_open else 'closed'} | US={'OPEN' if us_open else 'closed'}"


# ── Agent reasoning logger ────────────────────────────────────────────────────

def _append_reasoning_log(entry: dict) -> None:
    """Append a single run entry to the JSONL reasoning log."""
    def _update(data):
        if not isinstance(data, list):
            data = []
        data.append(entry)
        # Keep last 100 runs
        if len(data) > 100:
            data = data[-100:]
        return data

    try:
        locked_read_modify_write(AGENT_REASONING_LOG, _update, default=[])
    except Exception as e:
        log_warning(f"apex-agent: failed to write reasoning log: {e}")


def _summarize_input(tool_input: dict, max_chars: int = 200) -> dict:
    """Shrink a tool input dict for the decision trace — drop long text fields."""
    if not isinstance(tool_input, dict):
        return {}
    out = {}
    for k, v in tool_input.items():
        if isinstance(v, str) and len(v) > max_chars:
            out[k] = v[:max_chars] + '...'
        else:
            out[k] = v
    return out


def _compact(data, max_chars: int = 6000) -> str:
    """Compact JSON serialisation for tool results. Saves ~30% tokens vs pretty-print."""
    result_str = json.dumps(data, separators=(',', ':'))
    if len(result_str) > max_chars:
        return result_str[:max_chars] + '...[truncated]'
    return result_str


# ── Core agent loop ────────────────────────────────────────────────────────────

class ApexAgent:

    def __init__(self, mode: str, budget_usd: float = None, max_tool_calls: int = None):
        self.mode           = mode
        self.model          = MODEL_BY_MODE.get(mode, AGENT_MODEL)
        self.budget_usd     = budget_usd or BUDGET_BY_MODE.get(mode, 0.50)
        self.max_tool_calls = max_tool_calls or MAX_TOOL_CALLS_BY_MODE.get(mode, 20)
        self.tools          = generate_tool_definitions_for_mode(mode)
        self.messages       = []
        self.tool_calls_made = 0
        self.input_tokens   = 0
        self.output_tokens  = 0
        self.tools_called   = []  # list of names for the log
        self.decision_trace = []  # per-decision: reasoning text + tool + outcome
        self._confirmed_actions = set()  # apex names that received CONFIRM this run

        # Select provider — follow the global LLM provider flag
        try:
            from apex_llm_client import get_provider
            self._provider = get_provider()
        except Exception:
            self._provider = 'anthropic'

        if self._provider == 'gemini':
            self.model = GEMINI_MODEL_BY_MODE.get(mode, 'gemini-2.5-pro')
            self._init_gemini_client()
        else:
            # Anthropic (default)
            try:
                import anthropic as _anthropic
            except ImportError:
                raise ImportError("anthropic package not installed. Run: pip install anthropic")
            from apex_config import ANTHROPIC_API_KEY
            if not ANTHROPIC_API_KEY:
                raise ValueError('ANTHROPIC_API_KEY not set in .env.trading212')
            self._client = _anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=120)

    # ── Gemini backend ────────────────────────────────────────────────────────

    def _init_gemini_client(self):
        """Initialize google-genai client and convert tool defs to Gemini format."""
        try:
            from google import genai as _genai
            from google.genai import types as _gtypes
        except ImportError:
            raise ImportError("google-genai package not installed. Run: pip install google-genai")
        from apex_config import GEMINI_API_KEY
        if not GEMINI_API_KEY:
            raise ValueError('GEMINI_API_KEY not set in .env.trading212')
        self._gemini_client = _genai.Client(api_key=GEMINI_API_KEY)
        self._gtypes = _gtypes
        # Convert Anthropic-style tool defs → Gemini FunctionDeclarations
        # Deduplicate by name — Gemini rejects tool lists with duplicate function names
        # (can occur when the manifest and meta-tools share a name across SDK versions).
        fds = []
        _seen_names: set = set()
        for t in self.tools:
            tname = t['name']
            if tname in _seen_names:
                log_warning(f"apex-agent: duplicate tool name '{tname}' skipped in Gemini declarations")
                continue
            _seen_names.add(tname)
            fds.append(_gtypes.FunctionDeclaration(
                name=tname,
                description=t.get('description', ''),
                parameters=t.get('input_schema', {}),
            ))
        self._gemini_tools = [_gtypes.Tool(function_declarations=fds)]

    def _run_gemini(self, prompt: str) -> str:
        """Gemini function-calling agent loop (equivalent to Anthropic tool_use loop)."""
        _gtypes = self._gtypes
        from apex_agent_tools import estimate_cost_usd

        sys_instr = system_prompt(get_market_status())
        history = [_gtypes.Content(role='user', parts=[_gtypes.Part.from_text(text=prompt)])]
        final_text = ''

        while True:
            resp = self._gemini_client.models.generate_content(
                model=self.model,
                contents=history,
                config=_gtypes.GenerateContentConfig(
                    system_instruction=sys_instr,
                    tools=self._gemini_tools,
                    max_output_tokens=MAX_TOKENS,
                    http_options=_gtypes.HttpOptions(timeout=120_000),
                ),
            )

            # Token tracking
            meta = getattr(resp, 'usage_metadata', None)
            if meta:
                self.input_tokens  += getattr(meta, 'prompt_token_count', 0) or 0
                self.output_tokens += getattr(meta, 'candidates_token_count', 0) or 0

            # Budget check
            cost_so_far = estimate_cost_usd(self.input_tokens, self.output_tokens, self.model)
            if cost_so_far > self.budget_usd:
                log_warning(f"apex-agent [{self.mode}]: budget ${self.budget_usd:.2f} exceeded "
                            f"(${cost_so_far:.3f}), stopping loop")
                break

            candidate = resp.candidates[0] if resp.candidates else None
            if not candidate or not candidate.content:
                break
            parts = candidate.content.parts or []

            # Collect text
            step_reasoning = ''
            for part in parts:
                if hasattr(part, 'text') and part.text:
                    final_text = part.text
                    step_reasoning = part.text

            # Check for function calls
            fc_parts = [p for p in parts if getattr(p, 'function_call', None) is not None]
            if not fc_parts:
                break  # end_turn equivalent

            # Add model response to history
            history.append(candidate.content)

            # Execute all function calls, collect responses
            fn_response_parts = []
            for part in fc_parts:
                fc = part.function_call
                self.tool_calls_made += 1
                if self.tool_calls_made > self.max_tool_calls:
                    result_str = json.dumps({
                        'status': 'blocked',
                        'reason': f'Max tool calls ({self.max_tool_calls}) reached for this run.',
                    })
                else:
                    result_str = self._dispatch_tool(fc.name, dict(fc.args or {}))

                try:
                    _parsed = json.loads(result_str)
                    outcome = _parsed.get('status', 'unknown') if isinstance(_parsed, dict) else 'ok'
                except (ValueError, TypeError):
                    outcome = 'unparseable'
                self.decision_trace.append({
                    'step':      self.tool_calls_made,
                    'tool':      fc.name,
                    'input':     _summarize_input(dict(fc.args or {})),
                    'reasoning': step_reasoning[:500],
                    'outcome':   outcome,
                })

                fn_response_parts.append(
                    _gtypes.Part.from_function_response(
                        name=fc.name,
                        response={'result': result_str},
                    )
                )

            history.append(_gtypes.Content(role='user', parts=fn_response_parts))  # function responses

            if self.tool_calls_made >= self.max_tool_calls:
                log_warning(f"apex-agent [{self.mode}]: max tool calls ({self.max_tool_calls}) reached")
                break

        return final_text

    # ── Tool dispatcher ───────────────────────────────────────────────────────

    def _dispatch_tool(self, tool_name: str, tool_input: dict) -> str:
        """Execute one tool call. Returns JSON string for the tool_result message."""
        self.tools_called.append(tool_name)
        apex_name = _to_apex_name(tool_name)

        # Guard: Gemini can occasionally pass a list instead of a dict for tool_input
        # (e.g. when the model mis-formats a structured argument). Convert defensively.
        if not isinstance(tool_input, dict):
            tool_input = {} if tool_input is None else {'_raw': str(tool_input)}

        # ── Meta-tools ────────────────────────────────────────────────────────
        if tool_name == 'run_chain':
            chain = tool_input.get('chain_name', '')
            result = execute_chain(chain)
            return _compact(result)

        if tool_name == 'read_state_file':
            filename = tool_input.get('filename', '')
            fields = tool_input.get('fields', None)
            result = read_state_file(filename, fields)
            return _compact(result)

        if tool_name == 'send_telegram':
            message = tool_input.get('message', '')
            result = send_telegram_tool(message)
            return _compact(result)

        if tool_name == 'request_confirmation':
            action   = tool_input.get('action_description', '')
            timeout  = int(tool_input.get('timeout_seconds', 300))
            result   = request_confirmation(action, timeout)
            if result.get('confirmed'):
                self._confirmed_actions.add(apex_name)
            return json.dumps(result)

        if tool_name == 'write_agent_review':
            return self._write_agent_review(tool_input)

        if tool_name == 'tighten_stop':
            return self._tighten_stop(tool_input)

        if tool_name == 'close_position':
            return self._close_position(tool_input)

        if tool_name == 'log_agent_action':
            return self._log_agent_action(tool_input)

        # ── Apex tools ────────────────────────────────────────────────────────
        safety = _get_safety(apex_name)

        if safety in EXECUTE_TRADE_LEVELS:
            # Layer 1: confirm param must be True
            if not tool_input.get('confirm', False):
                return json.dumps({
                    'status': 'blocked',
                    'reason': f'[execute-trade] confirm=true required. '
                              'Call send_telegram to describe the action, then '
                              'request_confirmation, then retry with confirm=true.',
                })
            # Layer 2: must have received a confirmation this run
            if apex_name not in self._confirmed_actions:
                return json.dumps({
                    'status': 'blocked',
                    'reason': f'[execute-trade] No human confirmation received for {apex_name} '
                              'this session. Call request_confirmation first.',
                })
            # Layer 3: circuit breaker check
            cb = safe_read(os.path.join(LOGS_DIR, 'apex-circuit-breaker.json'), {})
            cb_status = cb.get('status', 'UNKNOWN') if isinstance(cb, dict) else 'UNKNOWN'
            if cb_status in ('SUSPEND', 'CRITICAL'):
                return json.dumps({
                    'status': 'blocked',
                    'reason': f'Circuit breaker is {cb_status}. No new trades allowed.',
                })
            # All layers passed — run with --force
            result = execute_apex_tool(apex_name, force=True)
        else:
            result = execute_apex_tool(apex_name, force=False)

        return _compact(result)

    # ── Agent loop ────────────────────────────────────────────────────────────

    def run(self, prompt: str) -> str:
        """
        Run the agent loop until stop_reason=='end_turn', budget exceeded,
        or max tool calls reached.
        Returns the final text response.
        Dispatches to Gemini backend when provider == 'gemini'.
        """
        if self._provider == 'gemini':
            return self._run_gemini(prompt)

        from apex_agent_tools import estimate_cost_usd

        self.messages = [{'role': 'user', 'content': prompt}]
        final_text    = ''

        while True:
            response = self._client.messages.create(
                model=self.model,
                system=system_prompt(get_market_status()),
                messages=self.messages,
                tools=self.tools,
                max_tokens=MAX_TOKENS,
            )

            self.input_tokens  += response.usage.input_tokens
            self.output_tokens += response.usage.output_tokens

            # Budget check
            cost_so_far = estimate_cost_usd(self.input_tokens, self.output_tokens, self.model)
            if cost_so_far > self.budget_usd:
                log_warning(f"apex-agent [{self.mode}]: budget ${self.budget_usd:.2f} exceeded "
                            f"(${cost_so_far:.3f}), stopping loop")
                break

            # Collect text from this response — this is the agent's reasoning
            # *before* the tool calls that follow in the same response.
            step_reasoning = ''
            for block in response.content:
                if hasattr(block, 'text') and block.text:
                    final_text = block.text  # keep last text block
                    step_reasoning = block.text  # reasoning for tools in this step

            if response.stop_reason == 'end_turn':
                break

            if response.stop_reason == 'tool_use':
                # Append assistant message
                self.messages.append({'role': 'assistant', 'content': response.content})

                # Execute all tool calls in this response
                tool_results = []
                for block in response.content:
                    if block.type != 'tool_use':
                        continue
                    self.tool_calls_made += 1
                    if self.tool_calls_made > self.max_tool_calls:
                        tool_results.append({
                            'type':        'tool_result',
                            'tool_use_id': block.id,
                            'content':     json.dumps({
                                'status': 'blocked',
                                'reason': f'Max tool calls ({self.max_tool_calls}) reached for this run.',
                            }),
                        })
                        continue

                    result_str = self._dispatch_tool(block.name, block.input)
                    tool_results.append({
                        'type':        'tool_result',
                        'tool_use_id': block.id,
                        'content':     result_str,
                    })

                    # Per-decision trace: reasoning + tool + input summary + outcome.
                    # Lets post-hoc analysis ask "why did the agent do X?" — the
                    # step_reasoning is the agent's text immediately before it called X.
                    try:
                        parsed = json.loads(result_str)
                        outcome = parsed.get('status', 'unknown') if isinstance(parsed, dict) else 'ok'
                    except (ValueError, TypeError):
                        outcome = 'unparseable'
                    self.decision_trace.append({
                        'step':       self.tool_calls_made,
                        'tool':       block.name,
                        'input':      _summarize_input(block.input),
                        'reasoning':  step_reasoning[:500],
                        'outcome':    outcome,
                    })

                self.messages.append({'role': 'user', 'content': tool_results})

                # Stop if we hit the tool call limit
                if self.tool_calls_made >= self.max_tool_calls:
                    log_warning(f"apex-agent [{self.mode}]: max tool calls ({self.max_tool_calls}) reached")
                    break
            else:
                # Unexpected stop reason — exit gracefully
                break

        return final_text

    def _write_agent_review(self, tool_input: dict) -> str:
        """Write the agent's signal verdict to apex-agent-review.json."""
        verdict   = tool_input.get('verdict', '')
        reasoning = tool_input.get('reasoning_summary', '')
        sig_ts    = tool_input.get('signal_timestamp', '')
        confidence = float(tool_input.get('confidence', 0.0))

        if verdict not in ('PROCEED', 'VETO', 'NEUTRAL'):
            return json.dumps({'status': 'error', 'error': f'Invalid verdict: {verdict}'})

        review = {
            'verdict':            verdict,
            'reasoning_summary':  reasoning,
            'signal_timestamp':   sig_ts,
            'confidence':         confidence,
            'reviewed_at':        datetime.now(timezone.utc).isoformat(),
            'human_override':     None,
        }
        try:
            atomic_write(AGENT_REVIEW_FILE, review)
            return json.dumps({'status': 'ok', 'verdict': verdict, 'written_to': AGENT_REVIEW_FILE})
        except Exception as e:
            return json.dumps({'status': 'error', 'error': str(e)})

    def _tighten_stop(self, tool_input: dict) -> str:
        """Autonomously tighten a stop — protective action, no confirmation needed."""
        t212_ticker = tool_input.get('t212_ticker', '')
        new_stop = tool_input.get('new_stop')
        reason = tool_input.get('reason', '')

        if not t212_ticker or new_stop is None:
            return json.dumps({'status': 'error', 'reason': 'Missing t212_ticker or new_stop'})

        # Import and call the tighten-stop tool directly (not via subprocess)
        try:
            sys.path.insert(0, SCRIPTS_DIR)
            from importlib import import_module
            mod = import_module('apex-agent-tighten-stop')
            result = mod.tighten_stop(t212_ticker, new_stop)
        except Exception as e:
            # Fallback to subprocess
            try:
                proc = subprocess.run(
                    ['/home/ubuntu/bin/python3',
                     os.path.join(SCRIPTS_DIR, 'apex-agent-tighten-stop.py'),
                     t212_ticker, str(new_stop)],
                    capture_output=True, text=True, timeout=30,
                )
                result = json.loads(proc.stdout) if proc.stdout.strip() else {
                    'status': 'error', 'reason': proc.stderr[:500]
                }
            except Exception as e2:
                result = {'status': 'error', 'reason': str(e2)}

        # Log the action if successful
        if result.get('status') == 'success':
            self._log_agent_action({
                'action_type': 'stop_tightened',
                'ticker': t212_ticker,
                'details': f"Stop {result.get('old_stop')} -> {result.get('new_stop')}. {reason}",
                'confidence': tool_input.get('confidence', 0.8),
            })

        return json.dumps(result)

    def _close_position(self, tool_input: dict) -> str:
        """Market-close a position. Gated: requires confirm=true AND prior
        request_confirmation AND non-critical circuit breaker."""
        t212_ticker = tool_input.get('t212_ticker', '')
        reason      = tool_input.get('reason', '')
        confirm     = bool(tool_input.get('confirm', False))

        if not t212_ticker or not reason:
            return json.dumps({'status': 'error',
                               'reason': 'Missing t212_ticker or reason'})
        if not confirm:
            return json.dumps({
                'status': 'blocked',
                'reason': '[execute-trade] confirm=true required. Describe the '
                          'close via send_telegram, call request_confirmation, '
                          'then retry with confirm=true.',
            })
        if 'close_position' not in self._confirmed_actions:
            return json.dumps({
                'status': 'blocked',
                'reason': '[execute-trade] No human confirmation received for '
                          'close_position this session. Call request_confirmation first.',
            })

        # Tier authority gate — Probation agents cannot close positions.
        tier_doc = safe_read(os.path.join(LOGS_DIR, 'apex-agent-tier.json'), {}) or {}
        tier = tier_doc.get('tier', 'Probation')
        if not (tier_doc.get('authority') or {}).get('may_close_positions', False):
            return json.dumps({
                'status': 'blocked',
                'reason': f'[tier={tier}] Authority insufficient to close positions. '
                          f'Promotion requires ≥20 attributed actions, 30d α ≥ 0, brier ≤ 0.25. '
                          f'See apex-agent-tier.json.',
            })

        # Circuit breaker check — closes ARE allowed on CRITICAL (flatten book),
        # but log a warning so the action_type is visible in the audit trail.
        cb = safe_read(os.path.join(LOGS_DIR, 'apex-circuit-breaker.json'), {})
        cb_status = cb.get('status', 'UNKNOWN') if isinstance(cb, dict) else 'UNKNOWN'

        try:
            proc = subprocess.run(
                ['/home/ubuntu/bin/python3',
                 os.path.join(SCRIPTS_DIR, 'apex-agent-close-position.py'),
                 t212_ticker, '--reason', reason, '--confirm'],
                capture_output=True, text=True, timeout=60,
            )
            result = json.loads(proc.stdout) if proc.stdout.strip() else {
                'status': 'error', 'reason': proc.stderr[:500] or 'no output'
            }
        except Exception as e:
            result = {'status': 'error', 'reason': str(e)}

        result['circuit_breaker'] = cb_status
        if result.get('status') == 'success':
            self._log_agent_action({
                'action_type': 'exit_recommended',
                'ticker':      t212_ticker,
                'details':     f"close_position executed. Reason: {reason}",
                'confidence':  tool_input.get('confidence', 0.9),
            })
        return json.dumps(result)

    def _log_agent_action(self, tool_input: dict) -> str:
        """Log an autonomous action for learning/track record."""
        action_type = tool_input.get('action_type', 'unknown')
        ticker = tool_input.get('ticker', '')
        details = tool_input.get('details', '')
        confidence = float(tool_input.get('confidence', 0.5))

        entry = {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'mode': self.mode,
            'action_type': action_type,
            'ticker': ticker,
            'details': details,
            'confidence': confidence,
        }

        actions_file = os.path.join(LOGS_DIR, 'apex-agent-actions.json')

        def _update(data):
            if not isinstance(data, list):
                data = []
            data.append(entry)
            if len(data) > 500:
                data = data[-500:]
            return data

        try:
            locked_read_modify_write(actions_file, _update, default=[])
            return json.dumps({'status': 'ok', 'action_logged': action_type})
        except Exception as e:
            return json.dumps({'status': 'error', 'reason': str(e)})

    def record_cost(self) -> None:
        """Record this run's token usage in the shared cost tracker."""
        try:
            from apex_llm_cost_tracker import record_cost
            record_cost(
                module=f'apex-agent-{self.mode}',
                model=self.model,
                input_tok=self.input_tokens,
                output_tok=self.output_tokens,
            )
        except Exception as e:
            log_warning(f"apex-agent: failed to record cost: {e}")


# ── CLI entrypoint ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Apex Claude Agent')
    parser.add_argument('--mode', default='interactive',
                        choices=['interactive', 'morning-analysis', 'eod-review',
                                 'intraday-check', 'signal-review',
                                 'exit-optimizer', 'post-trade-autopsy'],
                        help='Operating mode')
    parser.add_argument('--prompt', default='',
                        help='Override prompt (interactive mode only)')
    parser.add_argument('--force-enable', action='store_true',
                        help='Bypass the feature flag (for testing)')
    args = parser.parse_args()

    # ── Feature flag check ────────────────────────────────────────────────────
    if not args.force_enable and args.mode != 'interactive':
        if not is_agent_enabled():
            log_info(f"apex-agent [{args.mode}]: agent disabled (feature flag off). "
                     "Send 'AGENT ON' via Telegram or use --force-enable to bypass.")
            print(f"Agent disabled. Use 'AGENT ON' via Telegram to enable.")
            sys.exit(0)

    # ── Build prompt ──────────────────────────────────────────────────────────
    if args.mode == 'interactive':
        if not args.prompt:
            parser.error("--prompt required for interactive mode")
        prompt = args.prompt
    elif args.mode == 'signal-review':
        # Inject pending signal context into the prompt
        signal = safe_read(os.path.join(LOGS_DIR, 'apex-pending-signal.json'), None)
        if not signal:
            log_info("apex-agent [signal-review]: no pending signal — skipping")
            print("No pending signal to review.")
            sys.exit(0)
        # Summarise signal for the prompt (avoid dumping full JSON)
        sig_summary = json.dumps({
            k: signal.get(k) for k in [
                'name', 'signal_type', 'adjusted_score', 'rsi', 'entry', 'stop',
                'target1', 'ev_verdict', 'quantity', 'sector', 'currency',
                'reasons', 'generated_at', 'created_at', 'timestamp',
                'adjustments', 'confidence_pct',
            ] if signal.get(k) is not None
        }, indent=2)
        raw_template = task_prompt('signal-review')
        prompt = raw_template.replace('{signal_context}', sig_summary)
    elif args.mode == 'exit-optimizer':
        # Skip if no active positions
        positions = safe_read(os.path.join(LOGS_DIR, 'apex-positions.json'), [])
        if not isinstance(positions, list):
            positions = []
        active = [p for p in positions if p.get('status') in ('protected', 'entry_placed')]
        if not active:
            log_info("apex-agent [exit-optimizer]: no active positions — skipping")
            print("No active positions to optimise.")
            sys.exit(0)
        prompt = task_prompt(args.mode)
    elif args.mode == 'post-trade-autopsy':
        # Skip if no closes in the last 7 days (was: today-only, which missed almost every trade
        # because most closes happen via reconciler at unpredictable times).
        outcomes_file = os.path.join(LOGS_DIR, 'apex-outcomes.json')
        outcomes = safe_read(outcomes_file, {})
        trades = outcomes.get('trades', outcomes) if isinstance(outcomes, dict) else outcomes
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).strftime('%Y-%m-%d')
        recent_closes = [t for t in trades if isinstance(t, dict) and t.get('closed', '') >= cutoff]
        if not recent_closes:
            log_info("apex-agent [post-trade-autopsy]: no trades closed in last 7 days — skipping")
            print("No trades closed in last 7 days to analyse.")
            sys.exit(0)
        prompt = task_prompt(args.mode)
    else:
        prompt = task_prompt(args.mode)

    # ── Run ───────────────────────────────────────────────────────────────────
    run_id    = str(uuid.uuid4())[:8]
    started   = datetime.now(timezone.utc).isoformat() + 'Z'
    log_info(f"apex-agent [{args.mode}] run={run_id} starting")

    agent  = ApexAgent(mode=args.mode)
    errors = []

    try:
        result = agent.run(prompt)
    except Exception as e:
        result = ''
        errors.append(str(e))
        log_warning(f"apex-agent [{args.mode}] run={run_id} error: {e}")
        # Clean up any pending confirmation
        try:
            if os.path.exists(AGENT_CONFIRM_FILE):
                os.remove(AGENT_CONFIRM_FILE)
        except OSError:
            pass

    finished = datetime.now(timezone.utc).isoformat() + 'Z'

    # ── Record cost ───────────────────────────────────────────────────────────
    agent.record_cost()

    # ── Reasoning log ─────────────────────────────────────────────────────────
    from apex_agent_tools import estimate_cost_usd
    cost = estimate_cost_usd(agent.input_tokens, agent.output_tokens, agent.model)

    _append_reasoning_log({
        'run_id':         run_id,
        'mode':           args.mode,
        'started':        started,
        'finished':       finished,
        'tools_called':   agent.tools_called,
        'tool_count':     agent.tool_calls_made,
        'decision_trace': agent.decision_trace,
        'final_output':   result[:500] if result else '',
        'tokens':         {'input': agent.input_tokens, 'output': agent.output_tokens},
        'cost_usd':       round(cost, 4),
        'errors':         errors,
    })

    log_info(f"apex-agent [{args.mode}] run={run_id} done | "
             f"tools={agent.tool_calls_made} tokens={agent.input_tokens}+{agent.output_tokens} "
             f"cost=${cost:.3f}")

    # ── Update track record after autopsy or exit-optimizer ──────────────────
    if args.mode in ('post-trade-autopsy', 'exit-optimizer') and not errors:
        try:
            from importlib import import_module
            learning_mod = import_module('apex-agent-learning')
            learning_mod.calculate_track_record()
            log_info(f"apex-agent [{args.mode}] track record updated")
        except Exception as e:
            log_warning(f"apex-agent: failed to update track record: {e}")

    if result:
        print(result)

    return 0 if not errors else 1


if __name__ == '__main__':
    sys.exit(main())
