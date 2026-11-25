"""reseat: carry a claude code session into a new one, keeping the talk and dropping the weight.

  reseat                      pick a session of this folder's project, reseat it, open the copy
  reseat last                 the newest session here (the one you just /exit-ed)
  reseat <id|prefix|file>     that one

  inside claude code, the /reseat skill runs this on the session it was called from.

  --work [N]        keep every tool call, each result cut to its first N chars (default 300).
                    without it: talk only (asked when you pick from the list)
  --talk            talk only, don't ask
  --at N            start at turn N; everything before it stays behind
  --drop-turns L    leave these turns out, like 12-15,40
  --letter FILE     a note that goes on top, framed as a compact summary
  --uncompact       go back through native compactions: the talk before them returns,
                    their summaries go
  --title T         title in the resume picker (default: "↻ " + the original's)
  --cwd DIR         where you'll resume from (default: where the original ran)
  --no-note         don't tell the new session it was reseated
  --allow KIND      drop a kind the table doesn't know, this once. matches the start of the
                    kind's name, so --allow meta drops every meta prompt it doesn't know
  --keep KIND       carry an unknown user line as it is instead (--keep meta)
  --probe           after the checks, resume the copy once in print mode and ask it for
                    the first and last thing said; prints its answer and the real context
  --no-open         print the resume line (copied) instead of opening the copy
  --tab             open the copy in a new kitty tab and say so in one line (needs kitty remote control)
  --json            end with one json line: id, model, cwd, title, words (for the hook)
  --dry             everything but the write
  -v                the full table: every kind, what happened to it, the order fixes

  --list            the turns, numbered the way --at and --drop-turns count them
  --weigh           what the session's context is made of
  --show UUID       one entry of the session, values clipped
"""
import argparse, json, os, shutil, subprocess, sys, uuid

from . import kitty, pick, verify
from .kinds import SIDE_TYPES, classify
from .rebuild import Plan, dumps, link, rebuild, select, side_lines, turns
from .session import CHAIN_TYPES, Refuse, Session, find, project_dir, text_of
from .weigh import k, weigh

QUESTION = ('answer in two short lines, quoting rather than paraphrasing: the first thing i said to you in '
            'this conversation, and the last thing i said before this question.')


def parse(argv):
    ap = argparse.ArgumentParser(prog='reseat', description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
                                 usage='reseat [session] [options]  (reseat -h for all)')
    ap.add_argument('session', nargs='?')
    ap.add_argument('--work', nargs='?', type=int, const=300, metavar='N')
    ap.add_argument('--talk', action='store_true')
    ap.add_argument('--at', type=int)
    ap.add_argument('--drop-turns')
    ap.add_argument('--letter')
    ap.add_argument('--uncompact', action='store_true')
    ap.add_argument('--title')
    ap.add_argument('--cwd')
    ap.add_argument('--no-note', action='store_true')
    ap.add_argument('--allow', action='append', default=[])
    ap.add_argument('--keep', action='append', default=[])
    ap.add_argument('--probe', action='store_true')
    ap.add_argument('--dry', action='store_true')
    ap.add_argument('--no-open', action='store_true')
    ap.add_argument('--tab', action='store_true')
    ap.add_argument('--json', action='store_true')
    ap.add_argument('-v', '--verbose', action='store_true')
    ap.add_argument('--list', action='store_true')
    ap.add_argument('--weigh', action='store_true')
    ap.add_argument('--show')
    ap.add_argument('--all', action='store_true', help=argparse.SUPPRESS)
    return ap.parse_args(argv)


def main(argv=None):
    o = parse(sys.argv[1:] if argv is None else argv)
    try:
        return run(o)
    except Refuse as e:
        print(str(e), file=sys.stderr)
        return 2


def run(o):
    picked = not o.session or o.session == 'last'
    if o.session == 'last':
        path = pick.newest(os.getcwd())
    elif picked:
        path = pick.choose(os.getcwd(), 0 if o.all else 15)
    else:
        path = find(o.session, os.getcwd())
    s = Session(path)
    if s.bad:
        print(f'note  {len(s.bad)} lines of {s.id[:8]} are not json (first: line {s.bad[0]}), a torn write; skipped. '
              'if one was on the conversation, the chain walk refuses below', file=sys.stderr)

    if o.show:
        return show(s, o.show)
    chain = s.chain(o.uncompact)
    if o.list:
        for n, i, kind, ts, text in turns(chain):
            print(f"{n}\t{ts[:16].replace('T', ' ')}\t{kind}\t{' '.join(text.split())[:90]}")
        return 0
    if o.weigh:
        return weigh_report(s, chain)

    mode = 'work' if o.work is not None else 'talk'
    if picked and o.work is None and not o.talk and sys.stdin.isatty():
        tok = weigh(chain)
        guess = 'talk' if tok['talk'] > 0.25 * (tok['talk'] + tok['tool calls'] + tok['tool results']) else 'work'
        mode = pick.ask_mode(guess)
    letter = open(os.path.expanduser(o.letter), encoding='utf-8').read() if o.letter else ''
    cwd = os.path.abspath(os.path.expanduser(o.cwd)) if o.cwd else (s.cwd() or os.getcwd())
    plan = Plan(mode=mode, result_chars=o.work if o.work is not None else 300, allow=o.allow, keep=o.keep,
                uncompact=o.uncompact, letter=letter, note=not o.no_note, source_id=s.id, cwd=cwd)

    entries, notes = select(chain, o.at, o.drop_turns)
    lines, report = rebuild(entries, plan)
    sid = str(uuid.uuid4())
    template = next((x for x in chain if x.get('type') == 'user'), chain[0])
    linked = link(lines, sid, template, cwd if cwd != template.get('cwd') else None)
    title = o.title or ('↻ ' + (s.title() or first_words(chain) or s.id[:8]).removeprefix('↻ '))
    out_lines = linked + side_lines(sid, linked, title)

    unknown_side = sorted({x.get('type') for x in s.lines
                           if x.get('type') not in SIDE_TYPES and x.get('type') not in CHAIN_TYPES})

    d = project_dir(cwd)
    new_path = os.path.join(d, sid + '.jsonl')
    if not o.dry:
        os.makedirs(d, exist_ok=True)
        with open(new_path, 'x', encoding='utf-8') as f:
            for x in out_lines:
                f.write(dumps(x) + '\n')
        check_lines = verify.load_lines(new_path)
    else:
        check_lines = json.loads(json.dumps(out_lines))

    w_in = verify.talk_words(entries)
    w_out = verify.talk_words([x for x in check_lines if 'uuid' in x])
    if o.uncompact:
        w_in.pop('summary', None)
    problems = verify.shape(check_lines, sid, mode)
    word_bad = {who for who in set(w_in) | set(w_out) if w_in[who] != w_out[who]}
    changed, now_sha = s.changed_since_read()

    tok_in, tok_out = weigh(chain), weigh([x for x in check_lines if 'uuid' in x])
    p = print
    p(f'from  {s.id[:8]}  "{s.title() or first_words(chain)[:50]}"  ({k(s.real_context())} real context)')
    p(f'to    {sid[:8]}  "{title}"' + ('   (dry run, nothing written)' if o.dry else ''))
    p(f'mode  {mode}' + (f', results cut to {plan.result_chars} chars' if mode == 'work' else '')
      + (', through native compactions' if o.uncompact else ''))
    for n in notes:
        p(f'      {n}')
    p('words ' + '  '.join(f'{who} {w_in[who]:,}→{w_out[who]:,}' for who in ('user', 'assistant', 'others', 'summary')
                           if w_in[who] or w_out[who]) + ('   ✓ every word' if not word_bad else '   ✗ MISMATCH'))
    p('shape ' + ('✓' if not problems else '✗ ' + '; '.join(problems)))
    if changed:
        p(f'orig  sha256 {s.sha[:12]} changed to {now_sha[:12]} while we read it: the session is live and claude '
          'appended to it (we never open it for writing). the copy is of the moment we read.')
    else:
        p(f'orig  sha256 {s.sha[:12]}… same before and after, untouched')
    p(f'carry ~{k(sum(tok_out.values()))} tokens of conversation (was ~{k(sum(tok_in.values()))}), plus the '
      'system prompt, tools and CLAUDE.md that load fresh on resume')
    if unknown_side:
        p(f'side  line types i don\'t know, not part of the conversation, not copied: {", ".join(map(str, unknown_side))}')
    if o.verbose:
        verbose(entries, report)

    if word_bad or problems:
        if not o.dry:
            trash(new_path)
            p(f'\nthe checks failed, the new file went to the trash. the original was not touched.', file=sys.stderr)
        return 1
    if o.dry:
        return 0

    pick.remember({'from': s.id, 'from_sha256': s.sha, 'to': sid, 'mode': mode, 'cwd': cwd, 'at': o.at,
                   'drop_turns': o.drop_turns, 'uncompact': o.uncompact, 'words': dict(w_out),
                   'when': linked[-1].get('timestamp') if linked else None})
    model = s.model()
    line = f'cd {shell_quote(cwd)} && claude --resume {sid}' + (f' --model {model}' if model else '')
    if o.probe:
        probe(cwd, sid, model)
    p('')
    if o.tab:
        return tab_report(o, s, sid, model, cwd, title, mode, w_out, tok_out, line)
    if o.json:
        p(json.dumps({'id': sid, 'model': model, 'cwd': cwd, 'title': title, 'mode': mode, 'words': dict(w_out),
                      'from': s.id, 'real_before': s.real_context(), 'carry': int(sum(tok_out.values()))},
                     ensure_ascii=False))
        return 0
    if sys.stdout.isatty() and sys.stdin.isatty() and not o.no_open and shutil.which('claude'):
        p(line)
        p('opening it…', flush=True)
        os.chdir(cwd)
        os.execvp('claude', ['claude', '--resume', sid] + (['--model', model] if model else []))
    p(line)
    if sys.stdout.isatty() and copy(line):
        p('(copied)')
    return 0


def tab_report(o, s, sid, model, cwd, title, mode, w_out, tok_out, line):
    """the one line /reseat shows, after opening the copy in a new kitty tab."""
    words = ', '.join(f'{who} {n:,}' for who, n in w_out.items() if n)
    head = (f'reseated ({mode}): {k(s.real_context())} → ~{k(sum(tok_out.values()))} of conversation + the floor, '
            f'every word carried ({words}).')
    cmd = f'claude --resume {sid}' + (f' --model {model}' if model else '')
    opened = kitty.open_tab(cwd, cmd, title)
    if opened:
        print(f'{head}\nthe copy is open in a new tab; close this one when you like.')
    else:
        copied = not os.environ.get('RESEAT_NO_CLIPBOARD') and copy(line)
        print(f'{head}\nno kitty to open it in, ' + ('the line is copied:' if copied else 'resume with:') + f'\n{line}')
    return 0


def first_words(chain):
    for x in chain:
        if classify(x).name == 'human prompt':
            return ' '.join(text_of(x['message']['content']).split())[:60]
    return ''


def shell_quote(s):
    return s if all(c.isalnum() or c in '/._-~' for c in s) else "'" + s.replace("'", "'\\''") + "'"


def trash(path):
    if shutil.which('trash'):
        subprocess.run(['trash', path], check=False)
    elif sys.platform == 'darwin':
        subprocess.run(['osascript', '-e', f'tell app "Finder" to delete POSIX file "{path}"'],
                       check=False, capture_output=True)
    elif shutil.which('gio'):
        subprocess.run(['gio', 'trash', path], check=False)
    else:
        os.remove(path)


def copy(text):
    for cmd in (['pbcopy'], ['wl-copy'], ['xclip', '-selection', 'clipboard']):
        if shutil.which(cmd[0]):
            subprocess.run(cmd, input=text.encode(), check=False)
            return True
    return False


def verbose(entries, report):
    fates = {}
    for x in entries:
        kk = classify(x)
        fates[kk.name] = {'drop': 'dropped', 'talk': 'kept', 'queued': 'kept, as a user message',
                          'assistant': 'text kept, the rest dropped (tool calls kept in work mode)',
                          'result': 'dropped (trimmed in work mode)', 'summary': 'kept (dropped with --uncompact)',
                          'boundary': 'kept on top only', 'unknown': 'not in the table'}[kk.fate]
    print('\nby kind:')
    for name, n in sorted(report['kinds'].items(), key=lambda kv: -kv[1]):
        print(f'  {n:6}  {name:48} {fates.get(name, "")}')
    print('order fixes:')
    for name, n in report['order'].items():
        print(f'  {n:6}  {name}')


def weigh_report(s, chain):
    tok = weigh(chain)
    total = sum(tok.values())
    real = s.real_context()
    print(f'{s.id}  "{s.title() or ""}"')
    print(f'model {s.model()}   chain {len(chain)} entries   turns {len(turns(chain))}')
    for name, v in sorted(tok.items(), key=lambda kv: -kv[1]):
        print(f'  {name:28} {k(v):>7}  {v / total * 100 if total else 0:5.1f}%')
    print(f'  {"estimate":28} {k(total):>7}')
    if real:
        print(f'  {"real, last reply":28} {k(real):>7}   (the gap is the system prompt, tools, and slack)')
    return 0


def show(s, ref):
    x = s.by.get(ref) or next((y for y in s.lines if str(y.get('uuid', '')).startswith(ref)), None)
    if not x:
        raise Refuse(f'no entry {ref} in {s.id}')

    def clip(v):
        if isinstance(v, str):
            return v[:300] + (f'…[+{len(v) - 300}]' if len(v) > 300 else '')
        if isinstance(v, list):
            return [clip(z) for z in v[:8]] + ([f'…[+{len(v) - 8} items]'] if len(v) > 8 else [])
        if isinstance(v, dict):
            return {kk: clip(z) for kk, z in v.items()}
        return v
    print(f'kind: {classify(x).name}  ({classify(x).fate})')
    print(json.dumps(clip(x), indent=1, ensure_ascii=False))
    return 0


def probe(cwd, sid, model):
    cmd = ['claude', '-p', '--resume', sid, '--fork-session', '--no-session-persistence', '--output-format', 'json']
    if model:
        cmd += ['--model', model]
    print('\nprobe: resuming the copy once (a throwaway fork, nothing saved)…', flush=True)
    try:
        r = subprocess.run(cmd + [QUESTION], cwd=cwd, capture_output=True, text=True, timeout=600)
        out = json.loads(r.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError) as e:
        print(f'probe failed: {e}')
        return
    u = out.get('usage') or {}
    real = u.get('input_tokens', 0) + u.get('cache_read_input_tokens', 0) + u.get('cache_creation_input_tokens', 0)
    for line in (out.get('result') or '').strip().splitlines():
        print(f'  | {line}')
    print(f'probe real context {k(real)}' + ('  (error: ' + str(out.get('subtype')) + ')' if out.get('is_error') else ''))
