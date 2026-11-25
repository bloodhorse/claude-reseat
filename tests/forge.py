"""synthetic sessions, written the way claude code writes them, one trap at a time.

no real transcript goes into this repo. every shape here was copied from the format
(docs/format.md), with made-up words.
"""
import json, os, uuid

CWD = '/tmp/reseat-test/project'
MODEL = 'claude-opus-5-5'
USAGE = {'input_tokens': 3, 'cache_creation_input_tokens': 1000, 'cache_read_input_tokens': 120000, 'output_tokens': 50}
PNG = ('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==')


class Forge:
    def __init__(self, sid=None, cwd=CWD):
        self.sid = sid or str(uuid.uuid4())
        self.cwd = cwd
        self.lines = []
        self.last = None
        self.n = 0

    def _ts(self):
        self.n += 1
        return f'2026-11-20T10:{self.n // 60:02d}:{self.n % 60:02d}.000Z'

    def _base(self, kind, parent='last'):
        return {'parentUuid': self.last if parent == 'last' else parent, 'isSidechain': False, 'type': kind,
                'uuid': str(uuid.uuid4()), 'timestamp': self._ts(), 'userType': 'external', 'entrypoint': 'cli',
                'cwd': self.cwd, 'sessionId': self.sid, 'version': '2.1.330', 'gitBranch': 'main'}

    def add(self, x, move=True):
        self.lines.append(x)
        if move and 'uuid' in x:
            self.last = x['uuid']
        return x

    # ---------- people ----------

    def user(self, content, **extra):
        x = self._base('user')
        x['message'] = {'role': 'user', 'content': content}
        x.update(extra)
        return self.add(x)

    def human(self, text, **extra):
        return self.user(text, promptId=str(uuid.uuid4()), origin={'kind': 'human'}, **extra)

    def human_image(self, text):
        self.user([{'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': PNG}},
                   {'type': 'text', 'text': text}], origin={'kind': 'human'})
        return self.user([{'type': 'text', 'text': '[Image: source: /Users/me/Desktop/shot.png]'}], isMeta=True)

    def peer(self, text):
        return self.user(f'Another Claude session sent a message:\n<cross-session-message from="roof">{text}'
                         '</cross-session-message>', isMeta=True, origin={'kind': 'peer'})

    def reply(self, *blocks, msg_id=None, model=MODEL, parent='last', **extra):
        """one api message; claude code writes one line per content block."""
        msg_id = msg_id or 'msg_' + uuid.uuid4().hex[:20]
        out = []
        for b in blocks:
            x = self._base('assistant', parent)
            parent = 'last'
            x['message'] = {'model': model, 'id': msg_id, 'type': 'message', 'role': 'assistant', 'content': [b],
                            'stop_reason': 'tool_use' if b['type'] == 'tool_use' else 'end_turn', 'usage': USAGE}
            x['requestId'] = 'req_' + msg_id
            x.update(extra)
            out.append(self.add(x))
        return out

    # ---------- blocks ----------

    @staticmethod
    def text(s):
        return {'type': 'text', 'text': s}

    @staticmethod
    def thinking(s='', tokens=900):
        return {'type': 'thinking', 'thinking': s, 'signature': 'sig' + uuid.uuid4().hex}

    @staticmethod
    def call(name='Bash', **inp):
        return {'type': 'tool_use', 'id': 'toolu_' + uuid.uuid4().hex[:20], 'name': name, 'input': inp or {'command': 'ls'}}

    def result(self, call, content='ok', parent='last', is_error=False):
        x = self._base('user', parent)
        b = {'type': 'tool_result', 'tool_use_id': call['id'], 'content': content}
        if is_error:
            b['is_error'] = True
        x['message'] = {'role': 'user', 'content': [b]}
        x['toolUseResult'] = {'stdout': content if isinstance(content, str) else '', 'stderr': ''}
        x['sourceToolAssistantUUID'] = self.last
        return self.add(x, move=(parent == 'last'))

    # ---------- the harness ----------

    def attachment(self, kind, **fields):
        x = self._base('attachment')
        x['attachment'] = dict(type=kind, **fields)
        return self.add(x)

    def reread(self, path='/tmp/reseat-test/project/CLAUDE.md', body='# a project\n' + 'words ' * 400):
        x = self.attachment('instructions', files=[{'path': path, 'content': body}])
        x['rendered'] = [{'content': f'<system-reminder>\nContents of {path} (project):\n\n{body}\n</system-reminder>'}]
        return x

    def queued(self, prompt, kind='human', mode='prompt'):
        return self.attachment('queued_command', prompt=prompt, commandMode=mode, origin={'kind': kind} if kind else None)

    def system(self, subtype, **fields):
        x = self._base('system')
        x.update(subtype=subtype, content='', level='info', **fields)
        return self.add(x)

    def compact(self, summary='the summary of what came before', preserve=0):
        """a native compaction. preserve=n keeps the last n chain lines before it whole,
        the way claude code does: named in preservedSegment, spliced in after the summary
        by uuid on load, never reachable by a parent walk from the leaf."""
        x = self._base('system', parent=None)
        x.update(subtype='compact_boundary', content='Conversation compacted', level='info',
                 logicalParentUuid=self.last,
                 compactMetadata={'trigger': 'auto', 'preTokens': 160000, 'postTokens': 9000})
        tail = self.last
        self.add(x)
        s = self.user(summary, isCompactSummary=True, isVisibleInTranscriptOnly=True)
        if preserve:
            by = {y['uuid']: y for y in self.lines if 'uuid' in y}
            head = tail
            for _ in range(preserve - 1):
                head = by[head]['parentUuid']
            x['compactMetadata']['preservedSegment'] = {'headUuid': head, 'anchorUuid': s['uuid'], 'tailUuid': tail}
        return s

    def side(self, kind, **fields):
        return self.add(dict(type=kind, sessionId=self.sid, **fields), move=False)

    # ---------- out ----------

    def write(self, projects):
        d = os.path.join(projects, ''.join(c if c.isalnum() or c == '-' else '-' for c in self.cwd))
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, self.sid + '.jsonl')
        with open(path, 'w', encoding='utf-8') as f:
            for x in self.lines:
                f.write(json.dumps(x, separators=(',', ':')) + '\n')
        return path


def talk_session():
    """a plain evening: talk, a couple of tool rounds, a re-read dump, thinking, reminders."""
    f = Forge()
    f.side('ai-title', aiTitle='the lantern night')
    f.attachment('instructions', files=[{'path': '/x/CLAUDE.md', 'content': 'rules ' * 300}])
    f.human('hey claude, what do you make of the lantern')
    f.attachment('date', date='2026-11-20')
    f.reply(f.thinking(), f.text('it looks smudged on purpose, the glyphs circle it.'))
    f.system('turn_duration', durationMs=1200)
    f.human('go check the file then')
    c = f.call('Read', file_path='/x/lantern.md')
    f.reply(f.thinking(), f.text('reading it.'), c)
    f.result(c, 'line one\n' * 500)
    f.reply(f.text('it says the glyphs were drawn by hand.'))
    f.reread()
    f.human('nice. good night')
    f.reply(f.text('night, brother.'))
    f.side('last-prompt', lastPrompt='nice. good night', leafUuid=f.last)
    return f
