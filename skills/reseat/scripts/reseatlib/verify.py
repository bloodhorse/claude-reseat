"""checking the new session against the original, from the file on disk.

two checks, both must pass or the new file goes to the trash:
  words    the words of every kind that's talk, counted on the carried part of the
           original and on the new file, by who said them. they must match exactly.
  shape    one straight parent line, one session id, at most one compact boundary and
           only on top, a user message before the first reply, no two replies in a row,
           no attachments, and in talk mode no tool traffic at all. in work mode every
           tool call has its result in the very next message and every result its call.
"""
import collections, json

from .kinds import classify
from .rebuild import STUBS, rejection_words
from .session import blocks, text_of


def nwords(s):
    return len(s.split())


def talk_words(entries):
    """Counter: who -> words, over everything that is talk."""
    c = collections.Counter()
    for x in entries:
        if x.get('reseat') in ('note', 'letter'):
            continue
        k = classify(x)
        if k.fate in ('talk', 'summary'):
            body = (x.get('message') or {}).get('content')
        elif k.fate == 'queued':
            body = (x.get('attachment') or {}).get('prompt')
        elif k.fate == 'assistant':
            body = [b for b in blocks(x) if b.get('type') == 'text']
        elif k.fate == 'result':
            c['user'] += sum(nwords(w) for w in rejection_words(x))
            continue
        else:
            continue
        if isinstance(body, str):
            c[k.who] += nwords(body)
        else:
            c[k.who] += sum(nwords(b.get('text', '')) for b in body or []
                            if b.get('type') == 'text' and b.get('text') not in STUBS)
    return c


def load_lines(path):
    with open(path, encoding='utf-8') as f:
        return [json.loads(l) for l in f if l.strip()]


def shape(lines, sid, mode):
    """-> list of problems (empty means fine)."""
    bad = []
    chain = [x for x in lines if 'uuid' in x]
    if not chain:
        return ['no messages at all']
    prev = None
    for n, x in enumerate(chain):
        if x.get('parentUuid') != prev:
            bad.append(f'line {n}: parent is not the line before')
            break
        prev = x['uuid']
    if len({x['uuid'] for x in chain}) != len(chain):
        bad.append('duplicate uuids')
    if any(x.get('sessionId') != sid for x in lines):
        bad.append('a line carries another session id')
    bounds = [n for n, x in enumerate(chain) if x.get('subtype') == 'compact_boundary']
    if bounds and bounds != [0]:
        bad.append(f'compact boundary at {bounds}: only one, only at the top (the loader reads from the last)')
    msgs = [x for x in chain if x['type'] in ('user', 'assistant') and not x.get('isCompactSummary')]
    if msgs and msgs[0]['type'] != 'user':
        bad.append('the first message is a reply')
    for a, b in zip(msgs, msgs[1:]):
        if a['type'] == b['type'] == 'assistant':
            bad.append('two replies in a row')
            break
    if any(x['type'] == 'attachment' for x in chain):
        bad.append('an attachment survived')
    if any(x['type'] == 'system' and x.get('subtype') != 'compact_boundary' for x in chain):
        bad.append('a system line survived')
    for x in msgs:
        if x['type'] == 'assistant':
            types = {b.get('type') for b in blocks(x)}
            if not types:
                bad.append('an empty reply')
            if types - {'text', 'tool_use'}:
                bad.append(f'a reply still holds {sorted(types - {"text", "tool_use"})}')
            if mode == 'talk' and 'tool_use' in types:
                bad.append('a tool call in talk mode')
        if x['type'] == 'user' and mode == 'talk' and any(b.get('type') == 'tool_result' for b in blocks(x)):
            bad.append('a tool result in talk mode')
    if mode == 'work':
        for n, x in enumerate(msgs):
            calls = [b['id'] for b in blocks(x) if b.get('type') == 'tool_use']
            nxt = msgs[n + 1] if n + 1 < len(msgs) else None
            got = [b.get('tool_use_id') for b in blocks(nxt)] if nxt and nxt['type'] == 'user' else []
            if calls and sorted(calls) != sorted(i for i in got if i):
                bad.append(f'message {n}: calls {calls} are not answered by the next message')
            if x['type'] == 'user':
                ids = [b.get('tool_use_id') for b in blocks(x) if b.get('type') == 'tool_result']
                before = msgs[n - 1] if n else None
                have = {b.get('id') for b in blocks(before)} if before else set()
                if ids and not set(ids) <= have:
                    bad.append(f'message {n}: results without their call')
    return list(dict.fromkeys(bad))
