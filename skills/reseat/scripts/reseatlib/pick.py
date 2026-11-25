"""the sessions of one project, newest first, by title, last words and weight."""
import json, os, shutil, sys, time

from .session import project_dir
from .weigh import k

LEDGER = os.path.join(os.environ.get('RESEAT_STATE') or os.path.expanduser('~/.local/state/reseat'), 'ledger.jsonl')


def ledger():
    """{new id: entry} for every reseat made on this machine."""
    out = {}
    if os.path.exists(LEDGER):
        for line in open(LEDGER, encoding='utf-8'):
            try:
                e = json.loads(line)
                out[e['to']] = e
            except (json.JSONDecodeError, KeyError):
                pass
    return out


def remember(entry):
    os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
    with open(LEDGER, 'a', encoding='utf-8') as f:
        f.write(json.dumps(entry, ensure_ascii=False) + '\n')


def peek(path):
    """title, last words and real context without parsing every line."""
    title = ai = last = first = None
    usage_line = None
    with open(path, 'rb') as f:
        for line in f:
            if b'"custom-title"' in line or b'"ai-title"' in line or b'"last-prompt"' in line:
                try:
                    x = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if x.get('type') == 'custom-title':
                    title = x.get('customTitle') or title
                elif x.get('type') == 'ai-title':
                    ai = x.get('aiTitle') or ai
                elif x.get('type') == 'last-prompt':
                    last = x.get('lastPrompt') or last
            elif b'"type":"assistant"' in line and b'"usage"' in line and b'"isSidechain":true' not in line:
                usage_line = line
            elif first is None and b'"type":"user"' in line and b'"isMeta":true' not in line and b'tool_result' not in line:
                try:
                    c = json.loads(line).get('message', {}).get('content')
                    first = c if isinstance(c, str) else ' '.join(b.get('text', '') for b in c or [] if b.get('type') == 'text')
                except (json.JSONDecodeError, AttributeError):
                    pass
    real = None
    if usage_line:
        try:
            u = json.loads(usage_line)['message']['usage']
            real = u.get('input_tokens', 0) + u.get('cache_read_input_tokens', 0) + u.get('cache_creation_input_tokens', 0)
        except (json.JSONDecodeError, KeyError, TypeError):
            pass
    return {'title': title or ai, 'last': last, 'first': first, 'real': real}


def age(seconds):
    for unit, n in (('d', 86400), ('h', 3600), ('m', 60)):
        if seconds >= n:
            return f'{int(seconds // n)}{unit}'
    return 'now'


def sessions(cwd, limit=15):
    d = project_dir(cwd)
    if not os.path.isdir(d):
        return d, []
    files = [os.path.join(d, f) for f in os.listdir(d) if f.endswith('.jsonl')]
    files.sort(key=os.path.getmtime, reverse=True)
    return d, files[:limit] if limit else files


def newest(cwd):
    """the newest session of this folder's project that has a word in it."""
    d, files = sessions(cwd, 0)
    for path in files:
        p = peek(path)
        if p['title'] or p['first'] or p['real']:
            return path
    sys.exit(f'no session with a word in it for {cwd}\n(looked in {d})')


def one_line(s):
    return ' '.join((s or '').split())


def choose(cwd, limit=15):
    d, files = sessions(cwd, limit)
    if not files:
        sys.exit(f'no sessions for {cwd}\n(looked in {d})')
    marks = ledger()
    width = shutil.get_terminal_size((120, 20)).columns
    rows = []
    now = time.time()
    for path in files:
        n = len(rows) + 1
        sid = os.path.basename(path)[:-6]
        p = peek(path)
        if not (p['title'] or p['first'] or p['real']):
            continue   # opened and closed without a word
        title = p['title'] or one_line(p['first'])[:60] or '(untitled)'
        mark = '↻ ' if sid in marks and not title.startswith('↻') else ''
        weight = k(p['real']) if p['real'] else (marks[sid].get('mode', 'new') if sid in marks else '—')
        rows.append((n, age(now - os.path.getmtime(path)), weight, mark + title, one_line(p['last']), path))
    if not rows:
        sys.exit(f'no session in {d} has a word in it')
    tw = min(42, max(len(r[3]) for r in rows))
    print(f'sessions in {cwd}  (newest first, weight = real context of the last reply)\n')
    for n, a, w, title, last, _ in rows:
        head = f'{n:>3}  {a:>4}  {w:>6}  {title[:tw]:<{tw}}'
        room = width - len(head) - 4
        print(head + (f'  « {last[:room]}' if last and room > 10 else ''))
    print()
    try:
        ans = input('which one? (number, q to leave) ').strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    if not ans or ans.lower().startswith('q'):
        sys.exit(0)
    if not ans.isdigit() or not 1 <= int(ans) <= len(rows):
        sys.exit(f'"{ans}" is not on the list')
    return rows[int(ans) - 1][5]


def ask_mode(guess):
    try:
        ans = input(f'talk only, or keep the work? [t/w, enter = {guess}] ').strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    return {'t': 'talk', 'w': 'work'}.get(ans[:1], guess)
