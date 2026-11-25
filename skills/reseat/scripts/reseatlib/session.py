"""reading a claude code transcript: the bytes, the live chain, and what's written beside it.

the original is read once into memory, hashed, and never opened for writing.
"""
import hashlib, json, os, re, time

CHAIN_TYPES = ('user', 'assistant', 'attachment', 'system')
PROJECTS = os.environ.get('RESEAT_PROJECTS') or os.path.expanduser('~/.claude/projects')


class Refuse(SystemExit):
    """a reason not to go on, printed as is. exit code 2."""

    def __init__(self, msg):
        super().__init__(msg)
        self.code = 2
        self.msg = msg

    def __str__(self):
        return self.msg


def blocks(x):
    c = (x.get('message') or {}).get('content')
    return c if isinstance(c, list) else []


def text_of(content):
    if isinstance(content, str):
        return content
    return ' '.join(b.get('text', '') if b.get('type') == 'text' else f"[{b.get('type')}]" for b in content or [])


def project_dir(cwd):
    return os.path.join(PROJECTS, re.sub(r'[^A-Za-z0-9-]', '-', os.path.abspath(cwd)))


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


class Session:
    def __init__(self, path):
        self.path = os.path.abspath(path)
        st = os.stat(self.path)
        self.mtime, self.size = st.st_mtime, st.st_size
        with open(self.path, 'rb') as f:
            raw = f.read()
        self.sha = hashlib.sha256(raw).hexdigest()
        self.id = os.path.basename(self.path)[:-len('.jsonl')]
        self.lines, self.bad = [], []
        for n, line in enumerate(raw.decode('utf-8', errors='replace').splitlines(), 1):
            if not line.strip():
                continue
            try:
                self.lines.append(json.loads(line))
            except json.JSONDecodeError:
                self.bad.append(n)
        self.by = {x['uuid']: x for x in self.lines if 'uuid' in x}
        self._chain = None

    # ---------- the chain ----------

    def leaf(self):
        for x in reversed(self.lines):
            if 'uuid' in x and not x.get('isSidechain') and x.get('type') in CHAIN_TYPES:
                return x
        raise Refuse(f'{self.id}: no conversation in this file')

    def chain(self, uncompact=False):
        """the entries the model sees, oldest first: leaf back through parentUuid. with
        uncompact, a compact boundary is crossed through its logicalParentUuid, so the
        history before every native compaction comes back."""
        key = bool(uncompact)
        if self._chain and self._chain[0] == key:
            return self._chain[1]
        out, u, seen = [], self.leaf()['uuid'], set()
        while u:
            if u in seen:
                raise Refuse(f'{self.id}: the chain loops at {u}')
            seen.add(u)
            x = self.by.get(u)
            if x is None:
                raise Refuse(f'{self.id}: the chain is broken, parent {u} is not in the file')
            out.append(x)
            u = x.get('parentUuid')
            if not u and uncompact and x.get('subtype') == 'compact_boundary':
                u = x.get('logicalParentUuid')
        out.reverse()
        if not uncompact and out and out[0].get('subtype') == 'compact_boundary':
            out = self._splice_preserved(out)
        out = recover_parallel_results(self.lines, out)
        self._chain = (key, out)
        return out

    def _splice_preserved(self, out):
        """a native compaction can keep the last few messages before it whole: the
        boundary's preservedSegment names them (head..tail, tail being its logical
        parent) and the summary they get spliced in after (anchor). the loader splices
        them by uuid, so a parent walk never sees them. put them where the loader does."""
        seg = (out[0].get('compactMetadata') or {}).get('preservedSegment')
        if not seg:
            return out
        head, tail, anchor = seg.get('headUuid'), seg.get('tailUuid'), seg.get('anchorUuid')
        part, u = [], tail
        while u and u in self.by and len(part) < 10000:
            part.append(self.by[u])
            if u == head:
                break
            u = self.by[u].get('parentUuid')
        else:
            raise Refuse(f'{self.id}: the compaction kept a preserved segment, and it can\'t be walked from its tail '
                         'back to its head. --uncompact walks the original messages instead')
        at = next((n for n, x in enumerate(out) if x.get('uuid') == anchor), None)
        if at is None:
            raise Refuse(f'{self.id}: the preserved segment\'s anchor is not on the chain; try --uncompact')
        part.reverse()
        return out[:at + 1] + part + out[at + 1:]

    # ---------- what's written beside the chain ----------

    def side(self, kind, field):
        vals = [x.get(field) for x in self.lines if x.get('type') == kind and x.get(field)]
        return vals[-1] if vals else None

    def title(self):
        return self.side('custom-title', 'customTitle') or self.side('ai-title', 'aiTitle')

    def cwd(self):
        for x in reversed(self.lines):
            if x.get('cwd'):
                return x['cwd']
        return None

    def model(self):
        """the model to resume with: the last real reply's model, or what it asked for if
        that reply came from a fallback."""
        for x in reversed(self.chain()):
            if x.get('type') != 'assistant':
                continue
            m = (x.get('message') or {}).get('model')
            if not m or m.startswith('<'):
                continue
            return x.get('requestedModel') or m
        return None

    def real_context(self):
        """input tokens of the last recorded reply: what the context actually weighed."""
        for x in reversed(self.lines):
            u = (x.get('message') or {}).get('usage') if x.get('type') == 'assistant' else None
            if u and not x.get('isSidechain'):
                return u.get('input_tokens', 0) + u.get('cache_read_input_tokens', 0) + u.get('cache_creation_input_tokens', 0)
        return None

    def changed_since_read(self):
        """(changed, now_sha). a live session gets appended while we work; that's not us."""
        now = sha256(self.path)
        return now != self.sha, now

    def age(self):
        return time.time() - self.mtime


def recover_parallel_results(lines, chain):
    """parallel tool calls are written as consecutive assistant lines, and the result of
    every call but the last hangs off its own call line as a sibling of the next one, so
    a parent walk from the leaf misses it. splice those results back in, right after the
    last assistant line of the same api message."""
    on = {x['uuid'] for x in chain}
    answered = {b.get('tool_use_id') for x in chain for b in blocks(x) if b.get('type') == 'tool_result'}
    called = {b['id']: x['uuid'] for x in chain for b in blocks(x) if b.get('type') == 'tool_use'}
    extra = {}
    for x in lines:
        if x.get('type') != 'user' or x.get('uuid') in on or x.get('parentUuid') not in on or x.get('isSidechain'):
            continue
        ids = [b.get('tool_use_id') for b in blocks(x) if b.get('type') == 'tool_result']
        if ids and all(i in called and i not in answered for i in ids):
            extra.setdefault(called[ids[0]], []).append(x)
            answered.update(ids)
    if not extra:
        return chain
    out = []
    for n, x in enumerate(chain):
        out.append(x)
        msg_id = (x.get('message') or {}).get('id')
        nxt = chain[n + 1] if n + 1 < len(chain) else None
        same_next = nxt and nxt.get('type') == 'assistant' and (nxt.get('message') or {}).get('id') == msg_id
        if x.get('type') == 'assistant' and not same_next:
            k = n
            while k > 0 and chain[k - 1].get('type') == 'assistant' and (chain[k - 1].get('message') or {}).get('id') == msg_id:
                k -= 1
            for y in chain[k:n + 1]:
                out.extend(extra.pop(y['uuid'], []))
    if extra:
        raise Refuse(f'could not place {sum(map(len, extra.values()))} recovered parallel tool results')
    return out


def find(ref, cwd=None):
    """a session from a path, an id, or an id prefix. the cwd's project is searched first."""
    if ref.endswith('.jsonl') and os.path.exists(ref):
        return os.path.abspath(ref)
    if not re.fullmatch(r'[0-9a-fA-F-]{4,36}', ref):
        raise Refuse(f'"{ref}" is not a session file, id or id prefix')
    dirs = []
    if cwd:
        dirs.append(project_dir(cwd))
    dirs += [os.path.join(PROJECTS, d) for d in sorted(os.listdir(PROJECTS))]
    for d in dirs:
        if not os.path.isdir(d):
            continue
        hits = [f for f in os.listdir(d) if f.startswith(ref) and f.endswith('.jsonl')]
        if len(hits) > 1:
            raise Refuse(f'"{ref}" matches {len(hits)} sessions in {d}; give more of the id')
        if hits:
            return os.path.join(d, hits[0])
    raise Refuse(f'no session "{ref}" under {PROJECTS}')
