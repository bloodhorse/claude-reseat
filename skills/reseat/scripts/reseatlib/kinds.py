"""what every chain entry is, and what a reseat does with it.

the table is closed on purpose: a kind that isn't named here stops the run and gets named
back, instead of being guessed at. to teach it a new kind, add it to the right set below
(and a case to tests/test_kinds.py). see docs/format.md for what each kind looks like.

classify() returns a Kind: (name, fate, who).
  fates: talk       a user message that is part of the conversation; kept, images stubbed
         queued     a queued_command attachment carrying talk; becomes a user message
         assistant  a reply; text kept (and tool calls in work mode), the rest dropped
         result     a tool result; dropped in talk mode, trimmed in work mode
         summary    a compact summary (native or a letter); kept
         boundary   a compact boundary; kept only at the very top
         drop       machine text
         unknown    not in the table: refused unless --allow names it
  who:   user, assistant, others (other sessions, the field, schedules), summary, or None.
"""
import collections, re

from .session import text_of

Kind = collections.namedtuple('Kind', 'name fate who')

# attachments the harness writes next to the talk. all machine text, all dropped.
DROP_ATTACHMENTS = {
    'total_tokens_reminder', 'deferred_tools_record', 'batching_reminder_sent', 'environment',
    'bash_output_audience_note', 'prompt_snapshot', 'task_reminder', 'hook_success', 'date',
    'session_context', 'model', 'deferred_tools_delta', 'edited_text_file', 'skill_listing',
    'silent_turn_reminder', 'instructions', 'mcp_instructions_delta', 'credential_org',
    'agent_listing_delta', 'remote_session_change', 'diagnostics', 'auto_mode', 'elapsed_time_reminder',
    'hook_additional_context', 'file', 'thinking_drop', 'session_cron_carry', 'nested_memory',
    'date_change', 'compact_file_reference', 'inlined_image_paths', 'command_permissions',
    'read_truncation_notice', 'critical_system_reminder', 'thinking_stripped', 'structured_output',
    'hook_system_message', 'hook_non_blocking_error', 'plan_mode_exit', 'hook_cancelled', 'plan_mode',
    'task_status', 'plugin_note', 'fork_briefing', 'auto_mode_exit', 'plan_mode_reentry',
    'unknown_command_fallback', 'todo_reminder', 'hook_blocking_error', 'hook_stopped_continuation',
}

# system lines: bookkeeping the model never sees as talk. the boundary is handled apart.
DROP_SYSTEM = {
    'turn_duration', 'stop_hook_summary', 'local_command', 'informational', 'scheduled_task_fire',
    'api_error', 'model_refusal_no_fallback', 'model_refusal_fallback', 'bridge_status',
    'model_consent_fallback',
}

# lines that aren't on the chain at all: titles, modes, recordings, file history. never
# copied. one we haven't met is named in the report, not refused: it can't hold talk the
# model sees, because the model only sees the chain.
SIDE_TYPES = {
    'api-request-blob', 'api-request', 'api-request-shape', 'last-prompt', 'mode', 'atis-latch',
    'permission-mode', 'queue-operation', 'ai-title', 'custom-title', 'agent-name', 'bridge-session',
    'file-history-snapshot', 'file-history-delta', 'session-owner', 'cost-state', 'data-flow-records',
    'data-flow-incomplete', 'tools-used', 'continued-in', 'relocated', 'worktree-state', 'derived-from',
    'frame-link', 'artifact-comment-monitor', 'artifact-autoreact-ledger', 'summary',
}

ASSISTANT_TEXT = {'text'}
ASSISTANT_TOOL = {'tool_use'}
ASSISTANT_DROP = {'thinking', 'redacted_thinking', 'server_tool_use', 'web_search_tool_result',
                  'web_fetch_tool_result', 'code_execution_tool_result', 'mcp_tool_use', 'mcp_tool_result',
                  'fallback'}
USER_BLOCKS = {'text', 'image', 'document'}

MACHINE_TAGS = ('<command-name>', '<command-message>', '<local-command-stdout>', '<local-command-stderr>',
                '<local-command-caveat>', '<task-notification>')
# slash commands that are knobs, not talk, even with arguments
UI_COMMANDS = {'rename', 'model', 'effort', 'resume', 'status', 'config', 'remote-control', 'color', 'theme',
               'output-style', 'permissions', 'login', 'logout', 'add-dir', 'fast', 'vim', 'terminal-setup',
               'statusline', 'memory', 'mcp', 'hooks', 'agents', 'clear', 'compact', 'exit', 'cost', 'usage'}
NUDGES = ('Stop hook feedback', 'Continue from where you left off', '[Your previous response had no visible',
          'Your response above was stopped by a safety classifier', 'Your response above was cut off mid-stream',
          '[structured-output-enforce]')
PEER_PREFIX = 'Another Claude session sent a message:'
PEER_NOTICES = ('[Cross-session idle notice]', '[Cross-session delivery notice]')
SHELL_TAGS = ('<bash-input>', '<bash-stdout>', '<bash-stderr>')


def classify(x):
    t = x.get('type')
    if x.get('reseat') == 'note':
        return Kind('note from an earlier reseat', 'drop', None)
    if x.get('reseat') == 'results':
        return Kind('tool result', 'result', None)
    if t == 'system':
        st = x.get('subtype')
        if st == 'compact_boundary':
            return Kind('compact boundary', 'boundary', None)
        if st in DROP_SYSTEM:
            return Kind(f'system:{st}', 'drop', None)
        return Kind(f'system:{st}', 'unknown', None)
    if t == 'assistant':
        return Kind('assistant', 'assistant', 'assistant')
    if t == 'attachment':
        return _attachment(x.get('attachment') or {})
    if t == 'user':
        return _user(x)
    return Kind(f'line:{t}', 'unknown', None)


def _attachment(a):
    at = a.get('type')
    if at in DROP_ATTACHMENTS:
        return Kind(f'attachment:{at}', 'drop', None)
    if at != 'queued_command':
        return Kind(f'attachment:{at}', 'unknown', None)
    s = text_of(a.get('prompt')).lstrip()
    okind, mode = (a.get('origin') or {}).get('kind'), a.get('commandMode')
    if mode == 'task-notification' or okind == 'task-notification' or s.startswith('<task-notification>'):
        return Kind('queued task notice', 'drop', None)
    if okind == 'peer' or s.startswith(('<cross-session-message', PEER_PREFIX)):
        return Kind('queued message from another session', 'queued', 'others')
    if okind == 'channel' or s.startswith('<channel'):
        return Kind('queued channel message', 'queued', 'others')
    if okind in ('human', None) and mode in ('prompt', None):
        return Kind('queued prompt (typed while claude worked)', 'queued', 'user')
    return Kind(f'queued_command:{okind}/{mode}', 'unknown', None)


def _user(x):
    c = (x.get('message') or {}).get('content')
    s = text_of(c).lstrip()
    okind = (x.get('origin') or {}).get('kind')
    if x.get('isCompactSummary'):
        return Kind('compact summary', 'summary', 'summary')
    if isinstance(c, list) and any(b.get('type') == 'tool_result' for b in c):
        return Kind('tool result', 'result', None)
    if isinstance(c, list):
        odd = sorted({b.get('type') for b in c} - USER_BLOCKS)
        if odd:
            return Kind(f'user block:{",".join(map(str, odd))}', 'unknown', None)
    if okind == 'task-notification' or s.startswith('<task-notification>'):
        return Kind('task notice', 'drop', None)
    if okind == 'peer' or s.startswith(PEER_PREFIX):
        return Kind('message from another session', 'talk', 'others')
    if s.startswith(PEER_NOTICES):
        return Kind('cross-session notice', 'drop', None)
    if okind == 'channel' or s.startswith('<channel'):
        return Kind('channel message', 'talk', 'others')
    if x.get('scheduledTaskId'):
        return Kind('scheduled prompt (cron, loop)', 'talk', 'others')
    if x.get('sourceToolUseID'):
        return Kind('skill body loaded by a tool', 'drop', None)
    if s.startswith(MACHINE_TAGS):
        m = re.match(r'<command-name>/?([^<\s]+)</command-name>', s)
        if m and m.group(1) not in UI_COMMANDS and re.search(r'<command-args>\s*\S', s):
            return Kind('slash command with args', 'talk', 'user')
        return Kind('slash command / local output', 'drop', None)
    if x.get('isMeta'):
        if okind == 'auto-continuation' or s.startswith(NUDGES):
            return Kind('harness nudge', 'drop', None)
        if s.startswith('<system-reminder>'):
            return Kind('meta system reminder', 'drop', None)
        if re.match(r'\[\d+ prior /loop wakeups? found nothing', s):
            return Kind('loop wakeup note', 'drop', None)
        if isinstance(c, list) and c and all(b.get('type') in ('image', 'document') for b in c):
            return Kind('media a tool showed (pdf, picture)', 'drop', None)
        if s.startswith('Base directory for this skill:'):
            return Kind('skill body loaded by a slash command', 'drop', None)
        if s.startswith('[Image'):
            return Kind('image note', 'talk', 'user')
        return Kind(f'meta prompt starting {s[:40]!r}', 'unknown', None)
    if s.startswith('[Request interrupted'):
        return Kind('interrupt marker', 'talk', 'user')
    if s.startswith(SHELL_TAGS):
        return Kind('shell (!) line', 'talk', 'user')
    return Kind('human prompt', 'talk', 'user')


def turn_kind(x):
    """what opens a turn, for --list, --at and --drop-turns. None if x doesn't open one."""
    k = classify(x)
    return {'human prompt': 'user', 'queued prompt (typed while claude worked)': 'user, queued',
            'message from another session': 'other session', 'queued message from another session': 'other session, queued',
            'slash command with args': 'user, command', 'scheduled prompt (cron, loop)': 'schedule'}.get(k.name)
