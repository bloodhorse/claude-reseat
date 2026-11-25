"""rough weight of a chain, by what it's made of. chars/4 for prose, chars/2 plus
framing per block for tool traffic (code and json tokenize dense: a 252-call work copy
measured ~300 tokens a call), w*h/750 for images, the api's own count for thinking
(its text is stored empty). the real number is the last reply's usage
(Session.real_context()) or reseat --probe."""
import base64, collections, json, struct

from .session import blocks


def est(s):
    return len(s) / 4.0


def est_tool(s):
    return len(s) / 2.0 + 30


def image_dims(b64):
    try:
        raw = base64.b64decode(b64[:80000] + '=' * (-len(b64[:80000]) % 4))
    except Exception:
        return None
    if raw[:8] == b'\x89PNG\r\n\x1a\n':
        return struct.unpack('>II', raw[16:24])
    if raw[:2] == b'\xff\xd8':
        i = 2
        while i < len(raw) - 9:
            if raw[i] != 0xFF:
                i += 1
                continue
            m = raw[i + 1]
            if m in (0xC0, 0xC1, 0xC2):
                h, w = struct.unpack('>HH', raw[i + 5:i + 9])
                return w, h
            i += 2 + struct.unpack('>H', raw[i + 2:i + 4])[0]
    return None


def image_tokens(block):
    src = block.get('source') or {}
    dims = image_dims(src.get('data', '')) if src.get('type') == 'base64' else None
    if not dims:
        return 1600
    w, h = dims
    s = min(1.0, 1568 / max(w, h))
    return min(1600, int(w * s * h * s / 750)) or 1


def weigh(chain):
    """Counter: kind -> ~tokens."""
    tok = collections.Counter()
    seen_thinking = set()
    for x in chain:
        t = x.get('type')
        if t == 'attachment':
            a = x.get('attachment') or {}
            r = ''.join(z.get('content', '') if isinstance(z.get('content'), str) else json.dumps(z.get('content'))
                        for z in x.get('rendered') or [])
            tok['re-read instruction files' if a.get('type') == 'instructions' else 'attachments'] += est(r)
            continue
        m = x.get('message')
        if not isinstance(m, dict):
            continue
        c = m.get('content')
        if t == 'user':
            kind = 'summary' if x.get('isCompactSummary') else 'talk'
            if isinstance(c, str):
                tok[kind] += est(c)
                continue
            for b in c or []:
                bt = b.get('type')
                if bt == 'text':
                    tok[kind] += est(b.get('text', ''))
                elif bt == 'image':
                    tok['images'] += image_tokens(b)
                elif bt == 'tool_result':
                    rc = b.get('content')
                    if isinstance(rc, str):
                        tok['tool results'] += est_tool(rc)
                    else:
                        tok['tool results'] += 30
                        for z in rc or []:
                            if z.get('type') == 'image':
                                tok['images'] += image_tokens(z)
                            else:
                                tok['tool results'] += est_tool(z.get('text', '') or json.dumps(z)) - 30
        elif t == 'assistant':
            u = m.get('usage') or {}
            rid = x.get('requestId') or m.get('id')
            for b in c or []:
                bt = b.get('type')
                if bt == 'text':
                    tok['talk'] += est(b.get('text', ''))
                elif bt == 'tool_use':
                    tok['tool calls'] += est_tool(json.dumps(b.get('input', {})))
                elif bt in ('thinking', 'redacted_thinking') and rid not in seen_thinking:
                    seen_thinking.add(rid)
                    real = (u.get('output_tokens_details') or {}).get('thinking_tokens')
                    tok['thinking'] += real if real is not None else est(b.get('thinking', '') or b.get('data', ''))
    return tok


def k(n):
    """1234 -> 1.2k, 123456 -> 123k"""
    if n is None:
        return '?'
    return f'{n / 1000:.1f}k' if n < 10000 else f'{n / 1000:.0f}k'
