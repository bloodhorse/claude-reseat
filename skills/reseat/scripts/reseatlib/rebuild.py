"""turning the original's chain into the new session's lines.

two modes:
  talk  only the words: the user's, claude's text, other sessions'. everything else goes.
  work  the words plus every tool call as a real call, its input strings clipped and its
        result cut to the first N characters. claude remembers what it did and the
        start of what it saw.
"""
import collections, copy, json, re, uuid
from datetime import datetime, timezone

from .kinds import classify, turn_kind, ASSISTANT_TEXT, ASSISTANT_TOOL, ASSISTANT_DROP
from .session import Refuse, blocks, text_of

IMAGE_STUB = '[image dropped]'
DOC_STUB = '[document dropped]'
STUBS = {IMAGE_STUB, DOC_STUB}
NO_RESULT = '[no result was recorded for this call]'
INPUT_CLIP = 80
INPUT_KEY_CLIP = 300
INPUT_KEYS = {'command', 'file_path', 'path', 'pattern', 'url', 'query', 'description', 'glob', 'notebook_path',
              'skill', 'subagent_type', 'to', 'name'}
ZERO_USAGE = {'input_tokens': 0, 'output_tokens': 0, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 0}
COMMON = ('userType', 'entrypoint', 'cwd', 'version', 'gitBranch')
DROP_LINE_KEYS = ('toolUseResult', 'rendered', 'renderedRole', 'renderedBesideToolResult', 'renderedInHumanTurn',
                  'mcpMeta', 'imagePasteIds')


def now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'


# ---------- turns ----------

def turns(chain):
    """[(number, chain index, kind, timestamp, text)] for every turn start, from 1."""
    out = []
    for i, x in enumerate(chain):
        k = turn_kind(x)
        if k:
            body = (x.get('attachment') or {}).get('prompt') if x.get('type') == 'attachment' else (x.get('message') or {}).get('content')
            out.append((len(out) + 1, i, k, x.get('timestamp', ''), text_of(body)))
    return out


def parse_ranges(spec):
    nums = set()
    for part in str(spec).split(','):
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(r'(\d+)-(\d+)', part)
        if m:
            nums.update(range(int(m.group(1)), int(m.group(2)) + 1))
        elif part.isdigit():
            nums.add(int(part))
        else:
            raise Refuse(f'"{part}" is not a turn number or range (like 12-15,40)')
    return nums


def select(chain, at=None, drop=None):
    """the part of the chain to carry: from turn `at` on, minus the turns in `drop`.
    returns (entries, notes)."""
    T = turns(chain)
    by_num = {n: i for n, i, *_ in T}
    notes, start = [], 0
    if at:
        if int(at) not in by_num:
            raise Refuse(f'there is no turn {at}; this session has {len(T)} (see reseat --list)')
        start = by_num[int(at)]
        notes.append(f'cut before turn {at}: {start} chain entries left behind')
    gone = set()
    if drop:
        want = parse_ranges(drop)
        missing = sorted(want - set(by_num))
        if missing:
            raise Refuse(f'no such turns: {missing} (this session has {len(T)})')
        bounds = [i for _, i, *_ in T] + [len(chain)]
        for n in want:
            gone.update(range(bounds[n - 1], bounds[n]))
        notes.append(f'dropped turns {drop}: {len(gone - set(range(start)))} chain entries')
    return [x for j, x in enumerate(chain) if j >= start and j not in gone], notes


# ---------- pieces ----------

def stub_media(content):
    if isinstance(content, str):
        return content
    out = []
    for b in content or []:
        if b.get('type') == 'image':
            out.append({'type': 'text', 'text': IMAGE_STUB})
        elif b.get('type') == 'document':
            out.append({'type': 'text', 'text': DOC_STUB})
        else:
            out.append(b)
    return out


def rejection_words(result_line):
    """the user's typed words inside a rejected tool call, if any."""
    words = []
    for b in blocks(result_line):
        if b.get('type') != 'tool_result':
            continue
        s = text_of(b.get('content'))
        m = re.search(r'the user said:\n(.*?)(?:\n\nNote:|$)', s, re.S)
        if m and m.group(1).strip() and m.group(1).strip() != 'Denied by user':
            words.append(m.group(1).strip())
    return words


def clip_value(v, n=INPUT_CLIP):
    """a tool call's input, clipped. the fields that say what was done (the command, the
    file, the pattern) keep more; bulk fields (an edit's strings, a write's content) keep
    a hint."""
    if isinstance(v, str):
        return v if len(v) <= n else v[:n] + f'…[+{len(v) - n} chars cut by reseat]'
    if isinstance(v, list):
        return [clip_value(z, n) for z in v]
    if isinstance(v, dict):
        return {k: clip_value(z, INPUT_KEY_CLIP if k in INPUT_KEYS else n) for k, z in v.items()}
    return v


def trim_result(block, limit):
    """one tool_result block cut to `limit` characters of text; media become stubs."""
    c = block.get('content')
    if rejection_words({'message': {'content': [block]}}):
        return block
    parts = [{'type': 'text', 'text': c}] if isinstance(c, str) else list(c or [])
    out, left, cut = [], limit, 0
    for p in parts:
        t = p.get('type')
        if t == 'text':
            s = p.get('text', '')
            if left > 0:
                out.append({'type': 'text', 'text': s[:left]})
            cut += max(0, len(s) - max(left, 0))
            left -= len(s)
        elif t == 'image':
            out.append({'type': 'text', 'text': IMAGE_STUB})
        elif t == 'tool_reference':
            out.append({'type': 'text', 'text': f"[tool schema loaded: {p.get('tool_name', '?')}]"})
        else:
            out.append({'type': 'text', 'text': f'[{t} dropped]'})
    if cut:
        out.append({'type': 'text', 'text': f'…[+{cut} chars cut by reseat]'})
    if not out:
        out = [{'type': 'text', 'text': '(empty)'}]
    if all(p['type'] == 'text' for p in out):
        out = '\n'.join(p['text'] for p in out)
    nb = {'type': 'tool_result', 'tool_use_id': block.get('tool_use_id'), 'content': out}
    if block.get('is_error'):
        nb['is_error'] = True
    return nb


# ---------- the rebuild ----------

class Plan:
    def __init__(self, mode='talk', result_chars=300, allow=(), keep=(), uncompact=False, letter='', note=True,
                 source_id='', cwd=None):
        self.mode, self.result_chars, self.allow, self.keep = mode, result_chars, tuple(allow), tuple(keep)
        self.uncompact, self.letter, self.note = uncompact, letter.strip() if letter else '', note
        self.source_id, self.cwd = source_id, cwd


def note_text(plan):
    day = datetime.now().strftime('%Y-%m-%d')
    head = f'[reseat] this conversation was carried over from session {plan.source_id[:8]} on {day}. '
    if plan.mode == 'talk':
        body = ('only the words were kept: your thinking, your tool calls and what they returned, and every '
                'attachment were dropped. you remember what was said, not what you saw; look again before '
                'acting on a detail.')
    else:
        body = (f'your tool calls were kept, each result cut to its first {plan.result_chars} characters; '
                'thinking and attachments were dropped. you remember what you did and the start of what you '
                'saw; read a file again before you change it.')
    return head + body


def rebuild(entries, plan):
    """-> (lines, report). lines are unlinked dicts; link() gives them ids and parents."""
    kinds, unknown = collections.Counter(), collections.OrderedDict()
    out = []
    for n, x in enumerate(entries):
        k = classify(x)
        kinds[k.name] += 1
        fate = k.fate
        if fate == 'unknown':
            if any(k.name.startswith(a) for a in plan.allow):
                kinds[f'{k.name} (dropped by --allow)'] += 1
                continue
            if any(k.name.startswith(a) for a in plan.keep) and x.get('type') == 'user':
                kinds[f'{k.name} (kept as is by --keep)'] += 1
                out.append(copy.deepcopy(x))
                continue
            unknown.setdefault(k.name, []).append(x.get('uuid'))
            continue
        if fate == 'drop':
            continue
        if fate == 'boundary':
            if n == 0 and not plan.letter and not plan.uncompact:
                out.append(copy.deepcopy(x))
            continue
        if fate == 'summary':
            if not plan.uncompact:
                out.append(copy.deepcopy(x))
            continue
        if fate == 'talk':
            y = copy.deepcopy(x)
            y['message']['content'] = stub_media(y['message']['content'])
            out.append(y)
        elif fate == 'queued':
            a = x['attachment']
            y = {k2: copy.deepcopy(v) for k2, v in x.items() if k2 not in ('attachment',) + DROP_LINE_KEYS}
            y.update(type='user', message={'role': 'user', 'content': stub_media(copy.deepcopy(a.get('prompt')))})
            if k.who != 'user':
                y['isMeta'] = True
            if a.get('origin'):
                y['origin'] = a['origin']
            out.append(y)
        elif fate == 'assistant':
            m = x.get('message') or {}
            keep = []
            for b in m.get('content') or []:
                bt = b.get('type')
                if bt in ASSISTANT_TEXT:
                    if b.get('text', '').strip():
                        keep.append({'type': 'text', 'text': b['text']})
                elif bt in ASSISTANT_TOOL:
                    if plan.mode == 'work':
                        keep.append({'type': 'tool_use', 'id': b['id'], 'name': b['name'],
                                     'input': clip_value(b.get('input', {}))})
                elif bt not in ASSISTANT_DROP:
                    unknown.setdefault(f'assistant block:{bt}', []).append(x.get('uuid'))
            if keep:
                y = {k2: v for k2, v in x.items() if k2 not in DROP_LINE_KEYS}
                # the original's usage would tell claude code the context is as heavy as it was
                y['message'] = dict(m, content=keep, usage=ZERO_USAGE)
                out.append(y)
        elif fate == 'result':
            if plan.mode == 'work':
                res = [trim_result(b, plan.result_chars) for b in blocks(x) if b.get('type') == 'tool_result']
                y = {k2: v for k2, v in x.items() if k2 not in DROP_LINE_KEYS}
                y['message'] = {'role': 'user', 'content': res}
                out.append(y)
            else:
                words = rejection_words(x)
                if words:
                    kinds['user words inside a rejected tool call (kept as their message)'] += len(words)
                    y = {k2: v for k2, v in x.items() if k2 not in DROP_LINE_KEYS + ('sourceToolAssistantUUID',)}
                    y['message'] = {'role': 'user', 'content': '\n\n'.join(words)}
                    out.append(y)

    if unknown:
        lines = [f'  {len(v):5}  {name}   (first: {v[0]})' for name, v in unknown.items()]
        raise Refuse('refusing: these kinds are not in the table, and guessing could lose words or keep junk.\n'
                     + '\n'.join(lines) + '\n'
                     'look at one with:  reseat <session> --show <uuid>\n'
                     'then teach reseatlib/kinds.py, or run again with --allow <start of the kind> to drop it\n'
                     'this once, or --keep <start of the kind> to carry a user line as it is (--allow meta, --keep meta).')

    fixed, order = order_fix(out, plan)
    head = []
    if plan.letter:
        head = [{'type': 'system', 'subtype': 'compact_boundary', 'content': 'Conversation compacted',
                 'level': 'info', 'isSidechain': False,
                 'compactMetadata': {'trigger': 'manual', 'preTokens': 0, 'postTokens': 0}},
                {'type': 'user', 'isSidechain': False, 'message': {'role': 'user', 'content': plan.letter},
                 'isVisibleInTranscriptOnly': True, 'isCompactSummary': True, 'reseat': 'letter'}]
    start = 1 if fixed and fixed[0].get('subtype') == 'compact_boundary' else 0
    if start and fixed[1:2] and fixed[1].get('isCompactSummary'):
        start = 2
    rest = fixed[start:]
    if plan.note or (rest and rest[0]['type'] == 'assistant'):
        text = note_text(plan) if plan.note else '[the conversation before this point was dropped]'
        rest.insert(0, {'type': 'user', 'isMeta': True, 'isSidechain': False,
                        'message': {'role': 'user', 'content': text}, 'reseat': 'note'})
        order['note line added first'] = 1
    return head + fixed[:start] + rest, {'kinds': kinds, 'order': order}


def order_fix(out, plan):
    """fold runs of replies, pair every kept tool call with its result right after it."""
    order = collections.Counter()
    folded = []
    for x in out:
        if folded and x['type'] == 'assistant' and folded[-1]['type'] == 'assistant':
            prev = folded[-1]
            folded[-1] = dict(prev, message=dict(prev['message'], content=prev['message']['content'] + x['message']['content']))
            order['replies folded into the one before'] += 1
        else:
            folded.append(x)

    if plan.mode == 'work':
        paired, i = [], 0
        while i < len(folded):
            x = folded[i]
            i += 1
            if x['type'] != 'assistant':
                if is_result_line(x):
                    order['tool results with no call before them, dropped'] += 1
                    continue
                paired.append(x)
                continue
            paired.append(x)
            ids = [b['id'] for b in x['message']['content'] if b['type'] == 'tool_use']
            got, others = {}, []
            while i < len(folded) and folded[i]['type'] != 'assistant':
                y = folded[i]
                i += 1
                if is_result_line(y):
                    for b in y['message']['content']:
                        if b.get('type') == 'tool_result' and b.get('tool_use_id') in ids:
                            got.setdefault(b['tool_use_id'], b)
                        else:
                            order['tool results with no call before them, dropped'] += 1
                else:
                    others.append(y)
            if ids:
                content = []
                for tid in ids:
                    if tid not in got:
                        order['calls with no recorded result, given a stub'] += 1
                    content.append(got.get(tid) or {'type': 'tool_result', 'tool_use_id': tid, 'content': NO_RESULT})
                paired.append({'type': 'user', 'isSidechain': False, 'message': {'role': 'user', 'content': content},
                               'timestamp': x.get('timestamp', ''), 'reseat': 'results'})
            paired.extend(others)
        folded = paired

    for x in folded:
        if x['type'] == 'assistant':
            has_tool = any(b['type'] == 'tool_use' for b in x['message']['content'])
            x['message'] = dict(x['message'], stop_reason='tool_use' if has_tool else 'end_turn')
    order['user messages left back to back (claude code merges them)'] = sum(
        1 for a, b in zip(folded, folded[1:]) if a['type'] == b['type'] == 'user')
    return folded, order


def is_result_line(x):
    c = (x.get('message') or {}).get('content')
    return x.get('type') == 'user' and isinstance(c, list) and any(b.get('type') == 'tool_result' for b in c)


def link(lines, sid, template, cwd=None):
    """give every line a fresh uuid, a parent (one straight line), the new sessionId, and
    the fields claude code expects on lines we made ourselves.

    every line is stamped entrypoint 'cli', whatever the original said: a session born headless
    carries 'sdk-cli' on its lines, and the resume picker doesn't list a session like that."""
    common = {k: template[k] for k in COMMON if k in template}
    common['entrypoint'] = 'cli'
    if cwd:
        common['cwd'] = cwd
    out, prev, ts = [], None, now()
    for x in lines:
        y = dict(x)
        y['uuid'] = str(uuid.uuid4())
        y['parentUuid'] = prev
        y['sessionId'] = sid
        if 'session_id' in y:
            y['session_id'] = sid
        y.pop('logicalParentUuid', None)
        for k, v in common.items():
            if k in ('cwd', 'entrypoint') or k not in y:
                y[k] = v
        y.setdefault('timestamp', ts)
        y.setdefault('isSidechain', False)
        prev = y['uuid']
        out.append(y)
    return out


def side_lines(sid, lines, title):
    """what the resume picker reads: a title and the last prompt."""
    out = [{'type': 'custom-title', 'customTitle': title, 'sessionId': sid}]
    last = next((x for x in reversed(lines) if x['type'] == 'user' and not x.get('isMeta')
                 and classify(x).name in ('human prompt', 'slash command with args')), None)
    if last:
        s = ' '.join(text_of(last['message']['content']).split())
        out.append({'type': 'last-prompt', 'lastPrompt': s[:200], 'leafUuid': lines[-1]['uuid'], 'sessionId': sid})
    return out


def dumps(x):
    return json.dumps(x, ensure_ascii=False, separators=(',', ':'))
