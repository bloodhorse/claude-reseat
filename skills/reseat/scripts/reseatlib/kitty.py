"""opening the copy in a new kitty tab.

a session started in kitty has KITTY_LISTEN_ON in its environment. a background session
(claude --bg, attached with claude attach) runs in a daemon that doesn't, so then kitty's
own socket is found where a kitty.conf puts it (listen_on unix:/tmp/kitty-{kitty_pid})
and the os window that has focus gets the tab.
"""
import glob, json, os, re, shutil, stat, subprocess

PATHS = ':/Applications/kitty.app/Contents/MacOS:/opt/homebrew/bin'


def env():
    return {**os.environ, 'PATH': os.environ.get('PATH', '') + PATHS}


def kitten():
    return shutil.which('kitten', path=env()['PATH'])


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def sockets():
    """unix:/tmp/kitty-<pid> for every live kitty, the session's own first."""
    out = []
    if os.environ.get('KITTY_LISTEN_ON'):
        out.append(os.environ['KITTY_LISTEN_ON'])
    for path in glob.glob('/tmp/kitty-*'):
        m = re.fullmatch(r'/tmp/kitty-(\d+)', path)
        try:
            if m and stat.S_ISSOCK(os.stat(path).st_mode) and alive(int(m.group(1))):
                out.append('unix:' + path)
        except OSError:
            pass
    return list(dict.fromkeys(out))


def focused(socks, k):
    """the socket whose kitty has a focused os window, else the first."""
    for sock in socks:
        try:
            r = subprocess.run([k, '@', '--to', sock, 'ls'], capture_output=True, text=True, timeout=5, env=env())
            if r.returncode == 0 and any(w.get('is_focused') for w in json.loads(r.stdout)):
                return sock
        except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
            continue
    return socks[0] if socks else None


def open_tab(cwd, cmd, title):
    """True if a tab running `cmd` in `cwd` opened. when claude exits there, a shell stays.

    the tab gets no title of its own: a title set on a kitty tab sticks, and the tab then stops
    following the one claude code sends (its star, its working dots, the session's name)."""
    k = kitten()
    if not k or os.environ.get('RESEAT_NO_KITTY'):
        return False
    socks = sockets()
    sock = socks[0] if os.environ.get('KITTY_LISTEN_ON') else focused(socks, k)
    if not sock:
        return False
    r = subprocess.run([k, '@', '--to', sock, 'launch', '--type=tab', f'--cwd={cwd}',
                        '/bin/zsh', '-ic', f'{cmd}; exec zsh -i'],
                       capture_output=True, text=True, timeout=10, env=env())
    return r.returncode == 0
