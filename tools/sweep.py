#!/usr/bin/env python3
"""dry-run reseat over every transcript on this machine and tally what it refuses.

  python3 tools/sweep.py [--talk|--work] [--max-mb 80]

reads only (reseat --dry writes nothing). run it after a claude code update: a new
line kind shows up here as a refusal before it bites anyone. set RESEAT_SWEEP_SKIP to a regex of
project dir names to leave out (work projects that can hold live secrets, say).
"""
import collections, contextlib, io, os, re, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), 'skills', 'reseat', 'scripts'))
from reseatlib import cli  # noqa: E402
from reseatlib.session import PROJECTS  # noqa: E402

SKIP_RE = re.compile(os.environ['RESEAT_SWEEP_SKIP'], re.I) if os.environ.get('RESEAT_SWEEP_SKIP') else None


def main():
    mode = '--work' if '--work' in sys.argv else '--talk'
    max_mb = float(sys.argv[sys.argv.index('--max-mb') + 1]) if '--max-mb' in sys.argv else 80
    ok, refusals, examples, fails, crashes = 0, collections.Counter(), {}, [], []
    for d in sorted(os.listdir(PROJECTS)):
        if (SKIP_RE and SKIP_RE.search(d)) or not os.path.isdir(os.path.join(PROJECTS, d)):
            continue
        for f in os.listdir(os.path.join(PROJECTS, d)):
            p = os.path.join(PROJECTS, d, f)
            if not f.endswith('.jsonl') or os.path.getsize(p) > max_mb * 1e6:
                continue
            out, err = io.StringIO(), io.StringIO()
            try:
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = cli.main([p, '--dry', mode])
            except Exception as e:  # a crash is a bug in reseat, list it
                crashes.append((p, repr(e)))
                continue
            if code == 0:
                ok += 1
            elif code == 2:
                e = err.getvalue()
                hits = re.findall(r'^\s+\d+\s+(.*?)\s+\(first: (.*)\)$', e, re.M)
                for kind, first in hits:
                    refusals[kind] += 1
                    examples.setdefault(kind, f'{p} --show {first}')
                if not hits:
                    refusals['other: ' + re.sub(r'^[0-9a-f-]{36}: ', '', e.strip().splitlines()[-1])[:80]] += 1
            else:
                fails.append((p, [l for l in out.getvalue().splitlines() if l.startswith(('words', 'shape'))]))
    print(f'{mode[2:]}: {ok} clean, {sum(refusals.values())} refused, {len(fails)} failed a check, {len(crashes)} crashed')
    for kind, n in refusals.most_common():
        print(f'  {n:5}  {kind}')
        if kind in examples:
            print(f'         reseat {examples[kind]}')
    for p, lines in fails:
        print('FAILED', p, *lines, sep='\n  ')
    for p, e in crashes:
        print('CRASHED', p, e, sep='\n  ')
    return 1 if fails or crashes else 0


if __name__ == '__main__':
    sys.exit(main())
