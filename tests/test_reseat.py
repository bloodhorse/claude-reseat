"""run: python3 -m unittest discover -s tests   (from the repo root)

every test builds its own synthetic session in a temp dir; RESEAT_PROJECTS and
RESEAT_STATE point there, so nothing here can see ~/.claude.
"""
import contextlib, hashlib, io, json, os, shutil, sys, tempfile, unittest, unittest.mock

TMP = tempfile.mkdtemp(prefix='reseat-test-')
os.environ['RESEAT_PROJECTS'] = os.path.join(TMP, 'projects')
os.environ['RESEAT_STATE'] = os.path.join(TMP, 'state')
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'skills', 'reseat', 'scripts'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from forge import Forge, talk_session, CWD, MODEL  # noqa: E402
from reseatlib import cli, verify  # noqa: E402
from reseatlib.kinds import classify  # noqa: E402
from reseatlib.rebuild import IMAGE_STUB, NO_RESULT, Plan, rebuild, select  # noqa: E402
from reseatlib.session import Refuse, Session  # noqa: E402

PROJECTS = os.environ['RESEAT_PROJECTS']


def run(*args):
    """the cli, as a user would run it; -> (exit code, stdout, stderr, new session lines or None)."""
    out, err = io.StringIO(), io.StringIO()
    before = set(all_files())
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(args))
    new = sorted(set(all_files()) - before)
    lines = None
    if new:
        with open(new[0], encoding='utf-8') as f:
            lines = [json.loads(l) for l in f if l.strip()]
    return code, out.getvalue(), err.getvalue(), lines


def all_files():
    for d, _, files in os.walk(PROJECTS):
        for f in files:
            yield os.path.join(d, f)


def chain_of(lines):
    return [x for x in lines if 'uuid' in x]


def texts(lines):
    out = []
    for x in chain_of(lines):
        c = (x.get('message') or {}).get('content')
        if isinstance(c, str):
            out.append(c)
        else:
            out += [b.get('text', '') for b in c or [] if b.get('type') == 'text']
    return out


def sha(path):
    with open(path, 'rb') as fh:
        return hashlib.sha256(fh.read()).hexdigest()


class Talk(unittest.TestCase):
    def test_talk_keeps_every_word_and_nothing_else(self):
        f = talk_session()
        path = f.write(PROJECTS)
        h = sha(path)
        code, out, err, lines = run(f.sid, '--talk')
        self.assertEqual(code, 0, out + err)
        self.assertEqual(sha(path), h, 'the original changed')
        self.assertIn('✓ every word', out)
        self.assertIn('untouched', out)
        ch = chain_of(lines)
        self.assertEqual([x['type'] for x in ch], ['user', 'user', 'assistant', 'user', 'assistant', 'user', 'assistant'])
        self.assertEqual(ch[0].get('reseat'), 'note')
        reply = ch[4]['message']['content']
        self.assertEqual([b['text'] for b in reply], ['reading it.', 'it says the glyphs were drawn by hand.'])
        blob = json.dumps(lines)
        for gone in ('"type": "thinking"', 'tool_use', 'tool_result', 'rules rules', 'line one', 'turn_duration'):
            self.assertNotIn(gone, blob)
        self.assertEqual({x['sessionId'] for x in lines}, {lines[0]['sessionId']})
        self.assertNotEqual(lines[0]['sessionId'], f.sid)
        title = [x for x in lines if x['type'] == 'custom-title']
        self.assertEqual(title[0]['customTitle'], '↻ the lantern night')
        self.assertIn(f'--model {MODEL}', out)
        self.assertTrue(all(x['message']['usage']['input_tokens'] == 0 for x in ch if x['type'] == 'assistant'))

    def test_image_becomes_a_stub_and_its_note_stays(self):
        f = Forge()
        f.human_image('look at this')
        f.reply(f.text('i see a lantern.'))
        f.write(PROJECTS)
        code, out, err, lines = run(f.sid, '--talk')
        self.assertEqual(code, 0, out + err)
        t = texts(lines)
        self.assertIn(IMAGE_STUB, t)
        self.assertIn('[Image: source: /Users/me/Desktop/shot.png]', t)
        self.assertNotIn('base64', json.dumps(lines))

    def test_queued_messages(self):
        f = Forge()
        f.human('start')
        c = f.call()
        f.reply(c)
        f.result(c, 'out')
        f.queued('wait, also do this')
        f.queued('roof says hi', kind='peer')
        f.queued('<task-notification>\n<task-id>x</task-id></task-notification>', kind=None, mode='task-notification')
        f.reply(f.text('doing both.'))
        f.write(PROJECTS)
        code, out, err, lines = run(f.sid, '--talk')
        self.assertEqual(code, 0, out + err)
        ch = chain_of(lines)
        mine = [x for x in ch if x['message']['content'] == 'wait, also do this']
        peer = [x for x in ch if x['message']['content'] == 'roof says hi']
        self.assertEqual(len(mine), 1)
        self.assertFalse(mine[0].get('isMeta'))
        self.assertTrue(peer[0].get('isMeta'))
        self.assertEqual(peer[0]['origin'], {'kind': 'peer'})
        self.assertNotIn('task-notification', json.dumps(ch))
        self.assertNotIn('attachment', {x['type'] for x in ch})

    def test_peer_message_and_rejection_words(self):
        f = Forge()
        f.peer('go build the thing')
        c = f.call('Bash', command='rm -rf /')
        f.reply(f.text('on it.'), c)
        f.result(c, "The user doesn't want to proceed with this tool use. The tool use was rejected. "
                    "To tell you how to proceed, the user said:\nuse trash, you bass bird", is_error=True)
        f.reply(f.text('right, trash it is.'))
        f.write(PROJECTS)
        code, out, err, lines = run(f.sid, '--talk')
        self.assertEqual(code, 0, out + err)
        self.assertIn('use trash, you bass bird', texts(lines))
        self.assertIn('✓ every word', out)


class Traps(unittest.TestCase):
    def parallel(self):
        f = Forge()
        f.human('read both')
        c1, c2 = f.call('Read', file_path='/a'), f.call('Read', file_path='/b')
        a1, a2 = f.reply(f.thinking(), c1, c2, msg_id='msg_parallel')[1:]
        f.result(c1, 'AAA', parent=a1['uuid'])  # hangs off its own call, off the leaf's path
        f.result(c2, 'BBB')
        f.reply(f.text('both read.'))
        return f

    def test_parallel_results_are_found_in_work_mode(self):
        f = self.parallel()
        f.write(PROJECTS)
        code, out, err, lines = run(f.sid, '--work')
        self.assertEqual(code, 0, out + err)
        ch = chain_of(lines)
        calls = [b for x in ch if x['type'] == 'assistant' for b in x['message']['content'] if b['type'] == 'tool_use']
        self.assertEqual(len(calls), 2)
        res = [b for x in ch if x['type'] == 'user' and isinstance(x['message']['content'], list)
               for b in x['message']['content'] if b.get('type') == 'tool_result']
        self.assertEqual(sorted(b['content'] for b in res), ['AAA', 'BBB'])
        self.assertIn('shape ✓', out)

    def test_parallel_in_talk_mode(self):
        f = self.parallel()
        f.write(PROJECTS)
        code, out, err, lines = run(f.sid, '--talk')
        self.assertEqual(code, 0, out + err)
        self.assertEqual(texts(lines)[1:], ['read both', 'both read.'])

    def compacted(self):
        f = Forge()
        f.human('the very first thing')
        f.reply(f.text('heard.'))
        f.compact('summary: the user said the very first thing')
        f.human('after the compaction')
        f.reply(f.text('still here.'))
        return f

    def test_native_compaction_kept_on_top(self):
        f = self.compacted()
        f.write(PROJECTS)
        code, out, err, lines = run(f.sid, '--talk')
        self.assertEqual(code, 0, out + err)
        ch = chain_of(lines)
        self.assertEqual(ch[0]['subtype'], 'compact_boundary')
        self.assertTrue(ch[1].get('isCompactSummary'))
        self.assertEqual(ch[2].get('reseat'), 'note')
        self.assertNotIn('the very first thing', texts(lines)[3:])

    def test_uncompact_brings_back_what_came_before(self):
        f = self.compacted()
        f.write(PROJECTS)
        code, out, err, lines = run(f.sid, '--talk', '--uncompact')
        self.assertEqual(code, 0, out + err)
        t = texts(lines)
        self.assertEqual(t[1:], ['the very first thing', 'heard.', 'after the compaction', 'still here.'])
        self.assertNotIn('compact_boundary', json.dumps(lines))

    def preserved(self):
        f = Forge()
        f.human('one')
        f.reply(f.text('two'))
        f.human('three, kept whole')
        f.reply(f.text('four, kept whole'))
        f.compact('summary of one and two', preserve=2)
        f.human('five')
        f.reply(f.text('six'))
        return f

    def test_preserved_segment_is_spliced_after_the_summary(self):
        f = self.preserved()
        f.write(PROJECTS)
        code, out, err, lines = run(f.sid, '--talk')
        self.assertEqual(code, 0, out + err)
        self.assertEqual(texts(lines)[0], 'summary of one and two')
        self.assertEqual(texts(lines)[2:], ['three, kept whole', 'four, kept whole', 'five', 'six'])

    def test_preserved_segment_under_uncompact_comes_once(self):
        f = self.preserved()
        f.write(PROJECTS)
        code, out, err, lines = run(f.sid, '--talk', '--uncompact')
        self.assertEqual(code, 0, out + err)
        self.assertEqual(texts(lines)[1:], ['one', 'two', 'three, kept whole', 'four, kept whole', 'five', 'six'])

    def test_reread_dumps_go(self):
        f = talk_session()
        for _ in range(5):
            f.reread()
        f.human('still here?')
        f.write(PROJECTS)
        code, out, err, lines = run(f.sid, '--talk')
        self.assertEqual(code, 0, out + err)
        self.assertNotIn('words words', json.dumps(lines))

    def test_unknown_kind_is_refused_and_named(self):
        f = talk_session()
        f.attachment('brand_new_thing', text='who knows')
        f.human('and then')
        path = f.write(PROJECTS)
        code, out, err, lines = run(f.sid, '--talk')
        self.assertEqual(code, 2)
        self.assertIn('attachment:brand_new_thing', err)
        self.assertIsNone(lines, 'nothing may be written on a refusal')
        code, out, err, lines = run(f.sid, '--talk', '--allow', 'attachment:brand_new_thing')
        self.assertEqual(code, 0, out + err)

    def test_unknown_meta_prompt_is_refused(self):
        f = Forge()
        f.human('hi')
        f.user('the user ran /ask. a copy of you wrote this', isMeta=True)
        f.reply(f.text('ok'))
        f.write(PROJECTS)
        code, out, err, lines = run(f.sid, '--talk')
        self.assertEqual(code, 2)
        self.assertIn("meta prompt starting 'the user ran /ask", err)
        code, out, err, lines = run(f.sid, '--talk', '--keep', 'meta')
        self.assertEqual(code, 0, out + err)
        self.assertIn('the user ran /ask. a copy of you wrote this', texts(lines))
        code, out, err, lines = run(f.sid, '--talk', '--allow', 'meta')
        self.assertEqual(code, 0, out + err)
        self.assertNotIn('the user ran /ask. a copy of you wrote this', texts(lines))

    def test_skill_body_from_a_slash_command_is_dropped(self):
        f = Forge()
        f.human('hi')
        f.user('Base directory for this skill: /home/me/.claude/skills/reseat\n\n# reseat', isMeta=True)
        f.reply(f.text('ok'))
        f.write(PROJECTS)
        code, out, err, lines = run(f.sid, '--talk')
        self.assertEqual(code, 0, out + err)
        self.assertNotIn('Base directory for this skill', ''.join(texts(lines)))

    def test_unknown_assistant_block_is_refused(self):
        f = Forge()
        f.human('hi')
        f.reply({'type': 'hologram', 'data': 1}, f.text('ok'))
        f.write(PROJECTS)
        code, out, err, lines = run(f.sid, '--talk')
        self.assertEqual(code, 2)
        self.assertIn('assistant block:hologram', err)

    def test_unknown_side_line_is_named_not_refused(self):
        f = talk_session()
        f.side('some-new-ledger', v=1)
        f.write(PROJECTS)
        code, out, err, lines = run(f.sid, '--talk')
        self.assertEqual(code, 0, out + err)
        self.assertIn('some-new-ledger', out)

    def test_scheduled_prompts_and_nudges(self):
        f = Forge()
        f.human('watch the run')
        f.user('check the run, one bash call', isMeta=True, scheduledTaskId='abc123', promptSource='system')
        f.reply(f.text('run is fine.'))
        f.user('Stop hook feedback:\nsay something', isMeta=True)
        f.reply(f.text('said.'))
        f.user('<local-command-caveat>Caveat: ...</local-command-caveat>', isMeta=True)
        f.user('[1 prior /loop wakeup found nothing actionable]', isMeta=True)
        f.user([{'type': 'document', 'source': {'type': 'base64', 'media_type': 'application/pdf', 'data': 'JVBE'}}],
               isMeta=True)
        f.user('<command-name>/rename</command-name>\n<command-args>new name</command-args>')
        f.user('<command-name>/ask</command-name>\n<command-args>what is love</command-args>')
        f.reply(f.text('baby dont hurt me'))
        f.write(PROJECTS)
        code, out, err, lines = run(f.sid, '--talk')
        self.assertEqual(code, 0, out + err)
        t = ' '.join(texts(lines))
        self.assertIn('check the run', t)
        self.assertIn('what is love', t)
        self.assertNotIn('Stop hook', t)
        self.assertNotIn('/rename', t)
        self.assertNotIn('Caveat', t)
        self.assertNotIn('loop wakeup', t)
        self.assertNotIn('JVBE', json.dumps(lines))


class Work(unittest.TestCase):
    def test_work_mode_trims_and_pairs(self):
        f = Forge()
        f.human('fix it')
        big = f.call('Write', file_path='/x/a.py', content='x = 1\n' * 1000)
        f.reply(f.thinking(), f.text('writing.'), big)
        f.result(big, 'File created successfully')
        rd = f.call('Read', file_path='/x/a.py')
        f.reply(rd)
        f.result(rd, 'y' * 5000)
        img = f.call('Screenshot')
        f.reply(img)
        f.result(img, [{'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': 'AAAA'}}])
        lost = f.call('Bash', command='sleep 999')
        f.reply(lost)
        f.human('[Request interrupted by user for tool use]')
        f.reply(f.text('stopped.'))
        f.write(PROJECTS)
        code, out, err, lines = run(f.sid, '--work', '100')
        self.assertEqual(code, 0, out + err)
        self.assertIn('shape ✓', out)
        self.assertIn('✓ every word', out)
        blob = json.dumps(lines)
        self.assertNotIn('y' * 101, blob)
        self.assertIn('chars cut by reseat', blob)
        self.assertNotIn('x = 1\\nx = 1\\n' * 40, blob)
        self.assertIn(IMAGE_STUB, blob)
        self.assertIn(NO_RESULT, blob)
        self.assertNotIn('"type": "thinking"', blob)
        self.assertNotIn('toolUseResult', blob)

    def test_talk_typed_between_call_and_result_moves_after_the_result(self):
        f = Forge()
        f.human('go')
        c = f.call()
        f.reply(c)
        f.queued('also this')   # landed before the result line was written
        f.result(c, 'done')
        f.reply(f.text('ok, both.'))
        f.write(PROJECTS)
        code, out, err, lines = run(f.sid, '--work')
        self.assertEqual(code, 0, out + err)
        ch = chain_of(lines)
        i = next(n for n, x in enumerate(ch) if x['type'] == 'assistant')
        self.assertEqual(ch[i + 1]['message']['content'][0]['type'], 'tool_result')
        self.assertEqual(ch[i + 2]['message']['content'], 'also this')


class Cuts(unittest.TestCase):
    def five(self):
        f = Forge()
        for n in range(1, 6):
            f.human(f'turn {n}')
            f.reply(f.text(f'answer {n}'))
        return f

    def test_at(self):
        f = self.five()
        f.write(PROJECTS)
        code, out, err, lines = run(f.sid, '--talk', '--at', '4')
        self.assertEqual(code, 0, out + err)
        self.assertEqual(texts(lines)[1:], ['turn 4', 'answer 4', 'turn 5', 'answer 5'])

    def test_drop_turns(self):
        f = self.five()
        f.write(PROJECTS)
        code, out, err, lines = run(f.sid, '--talk', '--drop-turns', '2-3,5')
        self.assertEqual(code, 0, out + err)
        self.assertEqual(texts(lines)[1:], ['turn 1', 'answer 1', 'turn 4', 'answer 4'])

    def test_bad_turn_is_refused(self):
        f = self.five()
        f.write(PROJECTS)
        self.assertEqual(run(f.sid, '--talk', '--at', '9')[0], 2)
        self.assertEqual(run(f.sid, '--talk', '--drop-turns', 'x')[0], 2)

    def test_letter_sits_on_top_once(self):
        f = Forge()
        f.human('old')
        f.reply(f.text('old answer'))
        f.compact('native summary')
        f.human('new')
        f.reply(f.text('new answer'))
        f.write(PROJECTS)
        letter = os.path.join(TMP, 'letter.md')
        with open(letter, 'w') as fh:
            fh.write('dear claude, here is what happened.')
        code, out, err, lines = run(f.sid, '--talk', '--letter', letter)
        self.assertEqual(code, 0, out + err)
        ch = chain_of(lines)
        self.assertEqual([x.get('subtype') for x in ch].count('compact_boundary'), 1)
        self.assertEqual(ch[0].get('subtype'), 'compact_boundary')
        self.assertEqual(ch[1]['message']['content'], 'dear claude, here is what happened.')

    def test_list(self):
        f = self.five()
        f.write(PROJECTS)
        code, out, err, _ = run(f.sid, '--list')
        self.assertEqual(code, 0)
        self.assertEqual(len(out.strip().splitlines()), 5)
        self.assertIn('turn 3', out.splitlines()[2])


class Round(unittest.TestCase):
    def test_reseat_of_a_reseat(self):
        f = talk_session()
        f.write(PROJECTS)
        code, out, err, lines = run(f.sid, '--talk')
        self.assertEqual(code, 0, out + err)
        new_id = lines[0]['sessionId']
        code, out, err, lines2 = run(new_id, '--talk')
        self.assertEqual(code, 0, out + err)
        notes = [x for x in chain_of(lines2) if x.get('reseat') == 'note']
        self.assertEqual(len(notes), 1)
        self.assertIn(new_id[:8], notes[0]['message']['content'])
        self.assertEqual([x for x in lines2 if x['type'] == 'custom-title'][0]['customTitle'], '↻ the lantern night')
        self.assertEqual(texts(lines)[1:], texts(lines2)[1:])

    def test_dry_writes_nothing(self):
        f = talk_session()
        f.write(PROJECTS)
        code, out, err, lines = run(f.sid, '--talk', '--dry')
        self.assertEqual(code, 0, out + err)
        self.assertIsNone(lines)

    def test_model_prefers_what_was_asked_for(self):
        f = Forge()
        f.human('hi')
        f.reply(f.text('hey'), model='claude-opus-4-8', requestedModel='claude-fable-5')
        f.write(PROJECTS)
        self.assertEqual(Session(f.write(PROJECTS)).model(), 'claude-fable-5')

    def test_ledger_remembers(self):
        f = talk_session()
        f.write(PROJECTS)
        code, *_ = run(f.sid, '--talk')
        self.assertEqual(code, 0)
        with open(os.path.join(os.environ['RESEAT_STATE'], 'ledger.jsonl')) as fh:
            led = fh.read()
        self.assertIn(f.sid, led)


class Picker(unittest.TestCase):
    def test_list_shows_titles_and_skips_wordless_files(self):
        from reseatlib import pick
        cwd = '/tmp/reseat-test/picker'
        f = talk_session()
        f.cwd = cwd
        for x in f.lines:
            if 'cwd' in x:
                x['cwd'] = cwd
        f.write(PROJECTS)
        empty = Forge(cwd=cwd)
        empty.side('mode', mode='normal')
        empty.write(PROJECTS)
        p = pick.peek(f.write(PROJECTS))
        self.assertEqual(p['title'], 'the lantern night')
        self.assertEqual(p['last'], 'nice. good night')
        self.assertEqual(p['real'], 121003)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), unittest.mock.patch('builtins.input', return_value='q'):
            with self.assertRaises(SystemExit):
                pick.choose(cwd)
        rows = [l for l in out.getvalue().splitlines() if l.strip()[:1].isdigit()]
        self.assertEqual(len(rows), 1)
        self.assertIn('the lantern night', rows[0])


class Tab(unittest.TestCase):
    """what /reseat runs: reseat <its own id> --tab. here with no kitty and no clipboard."""

    def setUp(self):
        os.environ['RESEAT_NO_KITTY'] = '1'
        os.environ['RESEAT_NO_CLIPBOARD'] = '1'

    def tearDown(self):
        os.environ.pop('RESEAT_NO_KITTY', None)
        os.environ.pop('RESEAT_NO_CLIPBOARD', None)

    def test_last_paragraph_is_the_one_line(self):
        f = talk_session()
        f.write(PROJECTS)
        code, out, err, lines = run(f.sid, '--tab', '--talk')
        self.assertEqual(code, 0, out + err)
        last = out.strip().split('\n\n')[-1]
        self.assertTrue(last.startswith('reseated (talk)'), last)
        self.assertIn('every word carried', last)
        self.assertIn('claude --resume ' + lines[0]['sessionId'], last)

    def test_a_refusal_exits_2_with_the_reason(self):
        f = talk_session()
        f.attachment('brand_new_thing')
        f.human('more')
        f.write(PROJECTS)
        code, out, err, lines = run(f.sid, '--tab', '--talk')
        self.assertEqual(code, 2)
        self.assertIn('attachment:brand_new_thing', err)

    def test_socket_finder_skips_dead_kitties(self):
        from reseatlib import kitty
        self.assertFalse(kitty.alive(999999))
        for sock in kitty.sockets():
            self.assertTrue(sock.startswith('unix:'))


class Checks(unittest.TestCase):
    def test_shape_catches_breakage(self):
        f = Forge()
        f.human('a')
        f.reply(f.text('b'))
        f.reply(f.text('c'))
        for x in f.lines:
            x['sessionId'] = 'S'
        probs = verify.shape(f.lines, 'S', 'talk')
        self.assertTrue(any('two replies' in p for p in probs))
        probs = verify.shape(f.lines[1:], 'S', 'talk')
        self.assertTrue(any('parent' in p or 'first message' in p for p in probs))

    def test_shape_catches_an_unanswered_call(self):
        f = Forge()
        f.human('a')
        f.reply(f.call())
        f.human('b')
        for x in f.lines:
            x['sessionId'] = 'S'
        self.assertTrue(any('not answered' in p for p in verify.shape(f.lines, 'S', 'work')))

    def test_words_count_by_who(self):
        f = Forge()
        f.human('one two three')
        f.reply(f.text('four five'))
        f.peer('six')
        w = verify.talk_words(f.lines)
        self.assertEqual(w['user'], 3)
        self.assertEqual(w['assistant'], 2)
        self.assertGreater(w['others'], 1)

    def test_classify_covers_the_forge(self):
        f = talk_session()
        for x in f.lines:
            if 'uuid' in x:
                self.assertNotEqual(classify(x).fate, 'unknown', x)

    def test_a_headless_born_session_comes_out_stamped_cli(self):
        from reseatlib.rebuild import link
        lines = [{'type': 'user', 'entrypoint': 'sdk-cli', 'cwd': '/old', 'message': {'role': 'user', 'content': 'a'}},
                 {'type': 'assistant', 'entrypoint': 'sdk-cli', 'message': {'role': 'assistant', 'content': []}},
                 {'type': 'user', 'message': {'role': 'user', 'content': 'made by us, no stamp yet'}}]
        out = link(lines, 'S', {'entrypoint': 'sdk-cli', 'cwd': '/old', 'version': '1', 'userType': 'external'})
        self.assertEqual({x['entrypoint'] for x in out}, {'cli'})
        self.assertEqual(lines[0]['entrypoint'], 'sdk-cli')


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
