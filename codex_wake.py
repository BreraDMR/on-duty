#!/usr/bin/env python3
"""Дежурный Codex: wakes Codex threads up after a usage limit resets, so a long plan keeps going.

A thread gets on duty from its own chat (skill "dezhurstvo" runs `add`), then the daemon watches its
rollout. When the last turn died on usage_limit_exceeded and limits are back, it queues a "limit is
reset, carry on with the plan" message into the thread. The Codex desktop app picks queued messages up
by itself (checked 02.11.2026); a thread that isn't open in the app gets opened via codex://threads/<id>.

    codex_wake.py add <thread> [--mode review|recommended] [--plan PATH] [--name NAME]
    codex_wake.py add-items --plan PATH [--mode ...] [--name NAME] [--cwd DIR]
                                             a fresh run per plan item, no long thread (since 02.11.2026); the run goes
                                             to whichever seat has room: Codex, Claude 1, Claude 2 (since 06.11.2026)
    codex_wake.py seat 1|2|3 on|off          let item runs (and the session carousel) use that seat or not
    codex_wake.py remove|pause|resume <thread|all>
    codex_wake.py list | status [--json] | limits
    codex_wake.py wake <thread> [--force]    wake right now (skips the "was it limited" check with --force)
    codex_wake.py daemon                     the loop, run by launchd
    codex_wake.py on|off                     start / stop that launchd agent (off stays off after a reboot)
"""
import argparse, fcntl, glob, json, os, signal, subprocess, sys, time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

HOME = Path.home()
BASE = Path(__file__).resolve().parent
STATE = BASE / 'state'
WATCH = STATE / 'watch.json'
LIMITS_CACHE = STATE / 'limits.json'
LOG = STATE / 'codex-wake.log'
CODEX = '/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex'
SESSIONS = HOME / '.codex/sessions'
LOCKS = HOME / '.codex/thread-writer-locks'
QUEUE_DB = HOME / '.codex/queue_1.sqlite'
DONE_MARK = 'ПЛАН ЗАКРЫТ'

TICK = 60               # daemon loop, seconds
LIMITS_EVERY = 600      # how often to ask for limits while a thread waits, unless the reset is sooner
AFTER_RESET = 90        # grace after resetsAt before we poke anything
WAKE_CONFIRM = 300      # no new turn this long after a wake = it didn't take
WAIT_NOTICE = 180       # idle this long without "plan closed" = waiting for Damir

MODE_RULES = {
    'review': 'Режим ревью: проходи план, ищи проблемы, исправляй, коммить мелко.',
    'recommended': ('Режим «рекомендуемое»: на любой развилке сам бери свой первый, рекомендуемый вариант, не спрашивая, '
                    'и записывай каждое такое решение одной строкой в журнал решений (DECISIONS-dezhurstvo.md рядом с планом) — '
                    'Дамир просмотрит их потом разом.'),
}
HARD_STOPS = ('Без Дамира не делай и остановись с вопросом: запись в прод, текст наружу от его имени, '
              'деньги, удаление того, что не восстановить.')


def log(msg):
    STATE.mkdir(parents=True, exist_ok=True)
    line = f'{datetime.now():%Y-%m-%d %H:%M:%S} {msg}'
    with open(LOG, 'a') as f:
        f.write(line + '\n')
    if sys.stdout.isatty():
        print(line)


def notify(title, text):
    esc = lambda s: s.replace('\\', '\\\\').replace('"', '\\"')
    try:
        r = subprocess.run(['osascript', '-e', f'display notification "{esc(text)}" with title "{esc(title)}" sound name "Glass"'],
                           capture_output=True, text=True, timeout=10)
        if r.returncode:
            log(f'notify failed: {r.stderr.strip()[:200]}')
    except Exception as e:
        log(f'notify failed: {e!r}')


# ---------- watch list ----------

@contextmanager
def watch_list():
    """Read-modify-write of watch.json under a lock: the CLI (from a Codex chat) and the daemon both write it."""
    STATE.mkdir(parents=True, exist_ok=True)
    with open(STATE / 'watch.lock', 'w') as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            data = json.loads(WATCH.read_text())
        except (OSError, ValueError):
            data = {'threads': {}}
        yield data
        tmp = WATCH.with_suffix('.tmp')
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        os.replace(tmp, WATCH)


def read_watch():
    try:
        return json.loads(WATCH.read_text())
    except (OSError, ValueError):
        return {'threads': {}}


# ---------- codex side ----------

def rollout_path(tid):
    hits = glob.glob(str(SESSIONS / '*/*/*' / f'rollout-*-{tid}.jsonl'))
    return max(hits, key=os.path.getmtime) if hits else None


def thread_meta(tid):
    p = rollout_path(tid)
    if not p:
        return {}
    with open(p) as f:
        meta = json.loads(f.readline()).get('payload', {})
    return {'cwd': meta.get('cwd'), 'originator': meta.get('originator')}


def ts(s):
    return datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp()


def thread_state(tid):
    """Where the thread stands, from the tail of its rollout: running / limited / idle / done / missing."""
    p = rollout_path(tid)
    if not p:
        return {'state': 'missing'}
    size = os.path.getsize(p)
    chunk = 4 << 20
    with open(p, 'rb') as f:
        f.seek(max(0, size - chunk))
        lines = f.read().decode('utf-8', 'replace').splitlines()
    if size > chunk:
        lines = lines[1:]  # first one is probably cut in half
    last = None
    for line in reversed(lines):
        if '"event_msg"' not in line or not any(k in line for k in ('"task_started"', '"task_complete"', '"turn_aborted"')):
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get('type') == 'event_msg' and d['payload'].get('type') in ('task_started', 'task_complete', 'turn_aborted'):
            last = d
            break
    if not last:
        return {'state': 'idle', 'at': os.path.getmtime(p), 'msg': ''}
    pl, at = last['payload'], ts(last['timestamp'])
    if pl['type'] == 'task_started':
        return {'state': 'running', 'at': at}
    if pl['type'] == 'turn_aborted':
        return {'state': 'idle', 'at': at, 'msg': ''}
    err = pl.get('error') or {}
    if err.get('codex_error_info') == 'usage_limit_exceeded' or 'usage limit' in (err.get('message') or '').lower():
        return {'state': 'limited', 'at': at}
    msg = pl.get('last_agent_message') or ''
    if DONE_MARK in msg:
        return {'state': 'done', 'at': at, 'msg': msg}
    return {'state': 'idle', 'at': at, 'msg': msg}


def app_server_call(method, params=None, timeout=30):
    p = subprocess.Popen([CODEX, 'app-server'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True, bufsize=1)
    try:
        def send(m):
            p.stdin.write(json.dumps(m) + '\n')
            p.stdin.flush()

        def wait(i):
            end = time.time() + timeout
            while time.time() < end:
                line = p.stdout.readline()
                if not line:
                    raise RuntimeError('app-server exited')
                try:
                    m = json.loads(line)
                except ValueError:
                    continue
                if m.get('id') == i:
                    if 'error' in m:
                        raise RuntimeError(m['error'])
                    return m['result']
            raise TimeoutError(method)

        send({'id': 1, 'method': 'initialize', 'params': {
            'clientInfo': {'name': 'codex-wake', 'title': None, 'version': '1'}, 'capabilities': None}})
        wait(1)
        send({'method': 'initialized'})
        send({'id': 2, 'method': method, 'params': params})
        return wait(2)
    finally:
        p.terminate()
        try:
            p.wait(5)
        except subprocess.TimeoutExpired:
            p.kill()


def read_limits():
    """No tokens spent: the same numbers the app shows in its usage panel."""
    r = app_server_call('account/rateLimits/read')
    rl = r.get('rateLimits') or {}
    out = {'allowed': bool(r.get('ordinaryUsageAllowed')), 'checked_at': time.time(),
           'primary': rl.get('primary'), 'secondary': rl.get('secondary'),
           'free_resets': len([c for c in (r.get('rateLimitResetCredits') or {}).get('credits', [])
                               if c.get('status') == 'available'])}
    STATE.mkdir(parents=True, exist_ok=True)
    LIMITS_CACHE.write_text(json.dumps(out, indent=2))
    return out


def next_reset(lim):
    """When the window that is full (or fullest) frees up."""
    wins = [w for w in (lim.get('primary'), lim.get('secondary')) if w]
    full = [w for w in wins if (w.get('usedPercent') or 0) >= 100]
    pick = full or wins
    return max(w['resetsAt'] for w in pick) if pick else None


HANDOFF_STATE = Path(os.environ.get('DUTY_HANDOFF_DIR', HOME / '.claude/tools/handoff')).expanduser() / 'state'
CLAUDE_USAGE_CACHE = STATE / 'claude-usage.json'
CLAUDE_USAGE_EVERY = 300  # the menu asks every 20 s, the API gets asked at most this often
# keychain item of each account: the default config dir, and the sha256[:8] of CLAUDE_CONFIG_DIR for the second one
# Claude Code config dirs, one per account: the default one plus whatever DUTY_CLAUDE_DIRS adds (colon separated)
CLAUDE_DIRS = [HOME / '.claude'] + [Path(p).expanduser() for p in
                                    os.environ.get('DUTY_CLAUDE_DIRS', str(HOME / '.claude-account2')).split(':') if p]
CLAUDE_DIRS = [d for i, d in enumerate(CLAUDE_DIRS) if d.is_dir() and d not in CLAUDE_DIRS[:i]]


def keychain_item(config_dir):
    """Claude Code keeps each account's login under its own keychain name: the default dir has the plain one,
    any other CLAUDE_CONFIG_DIR gets the first 8 hex of sha256 of that path appended."""
    if config_dir == HOME / '.claude':
        return 'Claude Code-credentials'
    import hashlib
    return 'Claude Code-credentials-' + hashlib.sha256(str(config_dir).encode()).hexdigest()[:8]


CLAUDE_KEYCHAIN = {n: keychain_item(d) for n, d in enumerate(CLAUDE_DIRS, 1)}

MAC_MODE = STATE / 'mac-mode.json'
MAC_MODES = ('normal', 'duty', 'night')  # never hold / hold while a duty is live / hold till 08:00
NIGHT_UNTIL_HOUR = 8
BATTERY_FLOOR = 25        # on battery below this the mac is let go whatever the mode


def read_mac_mode():
    try:
        m = json.loads(MAC_MODE.read_text())
    except (OSError, ValueError):
        m = {}
    if m.get('mode') not in MAC_MODES:
        m = {'mode': 'normal'}
    if m['mode'] == 'night' and time.time() >= (m.get('until') or 0):
        m = {'mode': 'normal', 'expired': True}
    return m


def set_mac_mode(mode):
    m = {'mode': mode, 'at': time.time()}
    if mode == 'night':
        d = datetime.now().replace(hour=NIGHT_UNTIL_HOUR, minute=0, second=0, microsecond=0)
        if d.timestamp() <= time.time():
            d = datetime.fromtimestamp(d.timestamp() + 86400)
        m['until'] = d.timestamp()
    STATE.mkdir(parents=True, exist_ok=True)
    MAC_MODE.write_text(json.dumps(m))
    log(f'mac mode -> {mode}')


def battery():
    """(on battery?, percent) from pmset; (False, None) when it can't tell."""
    try:
        out = subprocess.run(['pmset', '-g', 'batt'], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return False, None
    import re
    pct = re.search(r'(\d+)%', out)
    return "'Battery Power'" in out, int(pct[1]) if pct else None


def sleep_holders():
    """Who keeps the mac from sleeping right now, by name, from pmset assertions."""
    import re
    try:
        out = subprocess.run(['pmset', '-g', 'assertions'], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    out = out.split('Listed by owning process:')[-1]
    names = []
    for block in re.split(r'\n(?=\s+pid \d+\()', out):
        m = re.search(r'pid (\d+)\(([^)]+)\): \[[^\]]*\] [\d:]+ (PreventUserIdleSystemSleep|PreventSystemSleep)', block)
        if not m:
            continue
        name = m[2]
        if name == 'powerd':
            continue  # "prevent sleep while display is on" — always there while you look at it
        if name == 'coreaudiod':
            name = 'звук или микрофон'
        elif name == 'caffeinate':
            who = re.search(r'on behalf of Process ID (\d+)', block)
            if who:
                cmd = subprocess.run(['ps', '-o', 'command=', '-p', who[1]], capture_output=True, text=True).stdout
                name = ('Дежурный' if 'codex_wake.py daemon' in cmd else 'сторож карусели' if 'watch-limit' in cmd
                        else Path(cmd.split()[0]).name if cmd.strip() else 'caffeinate')
            else:
                secs = re.search(r'asserting for (\d+) secs', block)
                name = f'caffeinate на {int(secs[1]) // 60} мин' if secs else 'caffeinate'
        if name not in names:
            names.append(name)
    return names


def mac_status(live=None):
    m = read_mac_mode()
    on_batt, pct = battery()
    holders = sleep_holders()
    return {'mode': m['mode'], 'until': m.get('until'), 'on_battery': on_batt, 'battery': pct,
            'battery_low': bool(on_batt and pct is not None and pct < BATTERY_FLOOR),
            'holding': 'Дежурный' in holders, 'holders': holders}


# the account carousel is a separate tool (not in this repo); without it the menu just hides that section
WATCH_LIMIT = HANDOFF_STATE.parent / 'watch-limit.py'
TOOL_ENV = {**os.environ, 'PATH': f'{HOME}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin'}


def claude_session(sid):
    """(pid, account, name) of a live claude session by its id (or a unique start of it) from /status."""
    sid = sid.strip().lower()
    hits = []
    for n, d in enumerate(CLAUDE_DIRS, 1):
        for f in (d / 'sessions').glob('*.json'):
            try:
                m = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            if sid and m.get('sessionId', '').startswith(sid) and pid_alive(m.get('pid')):
                hits.append((m['pid'], n, m.get('name') or '', m['sessionId']))
    return hits


def carousel_status():
    if not WATCH_LIMIT.exists():
        return None
    seats = {str(n): not (HANDOFF_STATE / f).exists() for n, f in ((1, 'off-account1'), (2, 'off-account2'), (3, 'off-codex'))}
    watchers = []
    for f in HANDOFF_STATE.glob('watch-*.pid'):
        try:
            pid = int(f.read_text())
        except (OSError, ValueError):
            continue
        if not pid_alive(pid):
            continue
        sid = f.stem[len('watch-'):]
        name = next((h[2] for h in claude_session(sid)), '')
        watchers.append({'sid': sid, 'name': name, 'pid': pid})
    return {'seats': seats, 'watchers': watchers}


def carousel_start(sid):
    hits = claude_session(sid)
    if len(hits) != 1:
        sys.exit('не нашёл живую сессию Claude с таким ID' if not hits else 'под это начало подходят несколько сессий — вставь ID целиком')
    pid, acc, name, full = hits[0]
    # 03.11 starting it switched every seat on; since 06.11 each seat has its own switch in the window, so leave them be
    r = subprocess.run([sys.executable, str(WATCH_LIMIT), '--pid', str(pid)], capture_output=True, text=True, env=TOOL_ENV,
                       cwd=str(HOME))
    log(f'carousel on for {full} (pid {pid}, account {acc}): {(r.stdout or r.stderr).strip()[:200]}')
    print((r.stdout or r.stderr).strip())


def carousel_stop(sid):
    r = subprocess.run([sys.executable, str(WATCH_LIMIT), '--sid', sid, '--stop'], capture_output=True, text=True, env=TOOL_ENV)
    log(f'carousel off for {sid}: {(r.stdout or r.stderr).strip()[:200]}')
    print((r.stdout or r.stderr).strip())


# ---------- seats: who runs the next plan item ----------
# Same switches as the session carousel (its off-* files), so one row of toggles in the window rules both.
SEATS = ('3', '1', '2')  # Codex first so Claude stays free for live sessions, then the personal account, then the work one
SEAT_NAMES = {'1': 'Claude 1', '2': 'Claude 2', '3': 'Codex'}
SEAT_OFF = {'1': 'off-account1', '2': 'off-account2', '3': 'off-codex'}
CLAUDE_BIN = HOME / '.local/bin/claude'
CLAUDE_FULL = 97        # a Claude window this full counts as gone, the run would just die on it
CLAUDE_REST = 900       # a Claude seat that said "limit" but gave no reset time rests this long
BROKEN_REST = 3600      # "not logged in" and the like: nothing to retry every minute


def seat_on(s):
    return not (HANDOFF_STATE / SEAT_OFF[s]).exists()


def set_seat(s, on):
    f = HANDOFF_STATE / SEAT_OFF[s]
    if on:
        f.unlink(missing_ok=True)
    else:
        HANDOFF_STATE.mkdir(parents=True, exist_ok=True)
        f.write_text(datetime.now().isoformat(timespec='seconds'))
    log(f'seat {s} ({SEAT_NAMES[s]}) switched {"on" if on else "off"}')


def claude_dir(s):
    """Config dir of a Claude seat, None when that account isn't set up on this mac."""
    i = int(s) - 1
    return CLAUDE_DIRS[i] if i < len(CLAUDE_DIRS) and CLAUDE_BIN.exists() else None


def claude_full_until(usage, now):
    """Reset time of the window that is full, None when there's room (or nothing is known: then just try)."""
    for w in (usage or {}).values():
        if (w.get('used_percentage') or 0) >= CLAUDE_FULL and (w.get('resets_at') or 0) > now:
            return w['resets_at']
    return None


def pick_seat(t, now, limits_ok):
    """First seat that is switched on, not resting after a limit and has room. Resting seats are written to t['rest']."""
    rest = t.setdefault('rest', {})
    for s in SEATS:
        if not seat_on(s) or rest.get(s, 0) > now:
            continue
        if s == '3':
            if limits_ok():
                return s
            continue
        if not claude_dir(s):
            continue
        until = claude_full_until((claude_usage_cached().get('accounts') or {}).get(s), now)
        if until:
            rest[s] = until + AFTER_RESET
            continue
        return s
    return None


def rest_seat(t, s, outcome, now):
    """A run on seat s hit the limit (or the seat is broken): keep off it for a while."""
    if outcome == 'broken':
        until = now + BROKEN_REST
    elif s == '3':
        until = now + LIMITS_EVERY  # exec says "limit" before the app-server does (02.11), so trust exec, then ask fresh
    else:
        reset = claude_full_until(claude_usage_api(int(s)), now)
        until = reset + AFTER_RESET if reset else now + CLAUDE_REST
    t.setdefault('rest', {})[s] = until
    log(f'seat {s} ({SEAT_NAMES[s]}) rests till {datetime.fromtimestamp(until):%d.%m %H:%M} ({outcome})')


# ---------- tokens since the last reset ----------
# Limits come as percents only; the token counts are summed from the local logs: every claude reply writes its usage,
# every codex step writes a token_count event. Scanned incrementally into 5-minute buckets, so a refresh is cheap.
TOKENS = STATE / 'tokens.json'
TOKENS_SCAN = STATE / 'tokens-scan.json'   # per-file offsets + buckets
TOKENS_LOCK = STATE / 'tokens.lock'
TOKENS_EVERY = 300
BUCKET = 300
KEEP = 8 * 86400


def _ts(s):
    return datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp()


def _scan_file(path, st, parse):
    """Read only what's new in the file since the last pass; parse(line, st) -> (ts, total, fresh) or None."""
    size = path.stat().st_size
    if size < st.get('off', 0):  # rewritten: start over
        st.clear()
    with open(path, 'rb') as f:
        f.seek(st.get('off', 0))
        data = f.read()
    end = data.rfind(b'\n') + 1  # a half-written last line waits for the next pass
    buckets = st.setdefault('b', {})
    for raw in data[:end].split(b'\n'):
        if not raw:
            continue
        try:
            hit = parse(raw, st)
        except Exception:
            hit = None
        if hit:
            ts, total, fresh = hit
            k = str(int(ts // BUCKET * BUCKET))
            b = buckets.setdefault(k, [0, 0])
            b[0] += total
            b[1] += fresh
    st['off'] = st.get('off', 0) + end


def _claude_line(raw, st):
    if b'"usage"' not in raw or b'"assistant"' not in raw:
        return None
    e = json.loads(raw)
    m = e.get('message') or {}
    u = m.get('usage')
    if e.get('type') != 'assistant' or not u or m.get('model') == '<synthetic>':
        return None
    key = f'{m.get("id")}|{e.get("requestId")}'  # one reply is several lines with the same usage
    seen = st.setdefault('seen', [])
    if key in seen[-50:]:
        return None
    seen.append(key)
    del seen[:-50]
    read = u.get('cache_read_input_tokens') or 0
    fresh = (u.get('input_tokens') or 0) + (u.get('output_tokens') or 0) + (u.get('cache_creation_input_tokens') or 0)
    return _ts(e['timestamp']), read + fresh, fresh


def _codex_line(raw, st):
    if b'"token_count"' not in raw or b'"total_token_usage"' not in raw:
        return None
    e = json.loads(raw)
    info = (e.get('payload') or {}).get('info') or {}
    tot, last = info.get('total_token_usage') or {}, info.get('last_token_usage') or {}
    if tot.get('total_tokens') == st.get('prev'):  # the same step reported twice
        return None
    st['prev'] = tot.get('total_tokens')
    inp, cached, out = last.get('input_tokens') or 0, last.get('cached_input_tokens') or 0, last.get('output_tokens') or 0
    return _ts(e['timestamp']), inp + out, inp - cached + out


def scan_tokens():
    try:
        db = json.loads(TOKENS_SCAN.read_text())
    except (OSError, ValueError):
        db = {}
    now = time.time()
    sources = [('claude', str(d), _claude_line) for d in [HOME / '.claude/projects']] + \
              [('codex', str(HOME / '.codex/sessions'), _codex_line)]
    for kind, root, parse in sources:
        files = db.setdefault(kind, {})
        for f in glob.glob(root + '/**/*.jsonl', recursive=True):
            path = Path(f)
            try:
                if path.stat().st_mtime < now - KEEP:
                    continue
            except OSError:
                continue
            _scan_file(path, files.setdefault(f, {}), parse)
        for f in list(files):  # drop old buckets and gone files
            b = files[f].get('b', {})
            for k in [k for k in b if int(k) < now - KEEP]:
                del b[k]
            if not Path(f).exists():
                del files[f]
    TOKENS_SCAN.write_text(json.dumps(db))
    return db


def _window_starts(resets, minutes):
    """Start of the current window from its reset time; a window already reset means it starts now."""
    if not resets:
        return time.time() - minutes * 60
    return min(resets - minutes * 60, time.time()) if resets > time.time() else time.time()


def count_tokens():
    db = scan_tokens()
    try:
        lim = json.loads(LIMITS_CACHE.read_text())
    except (OSError, ValueError):
        lim = {}
    # both claude accounts share one logs folder, so they're counted together, over the windows of account 1
    c1 = ((claude_usage_cached().get('accounts') or {}).get('1')) or {}
    starts = {
        'codex': {'five_hour': _window_starts((lim.get('primary') or {}).get('resetsAt'), 300),
                  'week': _window_starts((lim.get('secondary') or {}).get('resetsAt'), 10080)},
        'claude': {'five_hour': _window_starts((c1.get('five_hour') or {}).get('resets_at'), 300),
                   'week': _window_starts((c1.get('seven_day') or {}).get('resets_at'), 10080)},
    }
    out = {'at': time.time()}
    for kind, wins in starts.items():
        out[kind] = {}
        for win, start in wins.items():
            total = fresh = 0
            for st in db.get(kind, {}).values():
                for k, (t, f) in st.get('b', {}).items():
                    if int(k) + BUCKET > start:
                        total += t
                        fresh += f
            out[kind][win] = {'total': total, 'fresh': fresh, 'since': start}
    TOKENS.write_text(json.dumps(out))
    return out


def tokens_cached():
    """Last count; a stale one kicks off a background recount (the first full pass over the logs takes a while)."""
    try:
        t = json.loads(TOKENS.read_text())
    except (OSError, ValueError):
        t = None
    if (not t or time.time() - t.get('at', 0) > TOKENS_EVERY) and not (
            TOKENS_LOCK.exists() and time.time() - TOKENS_LOCK.stat().st_mtime < 900):
        STATE.mkdir(parents=True, exist_ok=True)
        TOKENS_LOCK.write_text(str(time.time()))
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), 'tokens'], start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return t


def claude_usage_api(n):
    """Same numbers as /usage, no tokens spent. Only reads the account's own login from the keychain and never
    refreshes it: an expired login (account off) just means no fresh numbers, the status line file stays the fallback."""
    import urllib.request
    try:
        raw = subprocess.run(['security', 'find-generic-password', '-s', CLAUDE_KEYCHAIN[n], '-w'],
                             capture_output=True, text=True, timeout=5).stdout
        oauth = json.loads(raw)['claudeAiOauth']
        if (oauth.get('expiresAt') or 0) / 1000 < time.time():
            return None
        req = urllib.request.Request('https://api.anthropic.com/api/oauth/usage', headers={
            'Authorization': f'Bearer {oauth["accessToken"]}', 'anthropic-beta': 'oauth-2025-04-20'})
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read())
    except Exception:
        return None
    out = {}
    for key in ('five_hour', 'seven_day'):
        w = d.get(key)
        if w and w.get('utilization') is not None:
            reset = datetime.fromisoformat(w['resets_at']).timestamp() if w.get('resets_at') else None
            out[key] = {'used_percentage': w['utilization'], 'resets_at': reset}
    return out or None


def claude_usage_cached():
    try:
        c = json.loads(CLAUDE_USAGE_CACHE.read_text())
    except (OSError, ValueError):
        c = {}
    if time.time() - c.get('at', 0) > CLAUDE_USAGE_EVERY:
        c = {'at': time.time(), 'accounts': {str(n): claude_usage_api(n) for n in CLAUDE_KEYCHAIN}}
        STATE.mkdir(parents=True, exist_ok=True)
        CLAUDE_USAGE_CACHE.write_text(json.dumps(c))
    return c
def find_agent():
    """The launchd agent that runs this very script as the daemon, whatever its label (install.sh makes one)."""
    import plistlib
    me = str(Path(__file__).resolve())
    for f in sorted((HOME / 'Library/LaunchAgents').glob('*.plist')):
        try:
            d = plistlib.loads(f.read_bytes())
        except Exception:
            continue
        args = d.get('ProgramArguments') or []
        if me in args and 'daemon' in args:
            return d.get('Label'), f
    return 'local.on-duty.watch', HOME / 'Library/LaunchAgents/local.on-duty.watch.plist'


LABEL, PLIST = find_agent()


def claude_accounts():
    """Claude limits for the menu, as the status line of each account last wrote them. They only move while
    a session of that account is open, so the age goes along, and a window whose reset has passed counts as empty."""
    now, out = time.time(), []
    api = claude_usage_cached()
    for n in CLAUDE_KEYCHAIN:
        f = HANDOFF_STATE / f'account{n}.json'  # what the carousel's status line wrote, the fallback
        rl = (api.get('accounts') or {}).get(str(n))
        if rl:
            updated = api['at']
        else:
            try:
                rl = json.loads(f.read_text()).get('rate_limits') or {}
                updated = f.stat().st_mtime
            except (OSError, ValueError):
                continue
        acc = {'account': n, 'updated_at': updated}
        for key, name in (('five_hour', 'five_hour'), ('seven_day', 'week')):
            w = rl.get(key)
            if not w:
                continue
            reset = w.get('resets_at')
            passed = bool(reset and reset < now)
            acc[name] = {'usedPercent': 0 if passed else w.get('used_percentage'), 'resetsAt': None if passed else reset,
                         'resetPassed': passed}
        out.append(acc)
    return out


def daemon_switch(on):
    """Off survives a reboot too: disable keeps launchd from loading the agent at login."""
    dom = f'gui/{os.getuid()}'
    if on:
        subprocess.run(['launchctl', 'enable', f'{dom}/{LABEL}'], capture_output=True)
        subprocess.run(['launchctl', 'bootstrap', dom, str(PLIST)], capture_output=True)
    else:
        subprocess.run(['launchctl', 'bootout', f'{dom}/{LABEL}'], capture_output=True)
        subprocess.run(['launchctl', 'disable', f'{dom}/{LABEL}'], capture_output=True)
    log(f'daemon switched {"on" if on else "off"}')


def wake_text(t):
    plan = f' по плану `{t["plan"]}`' if t.get('plan') else ' по плану'
    return (f'[Дежурный] Лимит сброшен. Коротко доложи, что уже сделано и что сейчас будет, и продолжай работу{plan}. '
            'Если твоя цель (goal) остановилась из-за лимита — верни её в active и продолжай. '
            f'{MODE_RULES.get(t.get("mode"), MODE_RULES["review"])} {HARD_STOPS} '
            f'Когда весь план выполнен — заверши цель и напиши последней строкой: {DONE_MARK}')


def queue_message(tid, text):
    """`codex queue` writes the message and then just sits there, so it gets ~15 s and a kill."""
    p = subprocess.Popen([CODEX, 'queue', '--thread', tid, '--message', text], stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    out = ''
    try:
        out, _ = p.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        p.kill()
        out, _ = p.communicate()
    ok = 'Queued message' in (out or '')
    if not ok:
        log(f'{tid} queue failed: {(out or "").strip()[:300]}')
    return ok


def open_in_app(tid):
    subprocess.run(['open', f'codex://threads/{tid}'], capture_output=True, timeout=15)


def loaded_in_app(tid):
    return (LOCKS / f'{tid}.lock').exists()


def wake(tid, t):
    if not queue_message(tid, wake_text(t)):
        return False
    if not loaded_in_app(tid):
        open_in_app(tid)  # the app sends queued messages only for threads it has loaded
    log(f'{tid} woken ({t.get("name")})')
    return True


# ---------- item runs: a fresh codex exec per plan item ----------
# Since 02.11.2026. One long thread re-sent 300-437k tokens of history on every step and burnt the 5h window
# in 17-33 min, so an "items" entry has no thread at all: each plan item gets its own `codex exec`, and the
# plan file is the only memory between them.

RUNS = STATE / 'runs'
ITEM_DONE, NEED_DAMIR, ITEM_QUESTION = 'ПУНКТ ГОТОВ', 'НУЖЕН ДАМИР', 'ПУНКТ ЖДЁТ РЕШЕНИЯ'
MAX_NO_PROGRESS = 2     # runs in a row that closed nothing = something's off, stop and tell Damir
MAX_FAILURES = 2        # runs in a row that ended with neither marker nor limit


def open_items(plan, mark=' '):
    """Items still to do ('- [ ]'), or with mark='?' the ones parked until Damir answers ('- [?]')."""
    try:
        return sum(1 for line in Path(plan).read_text().splitlines() if line.lstrip().startswith(f'- [{mark}]'))
    except OSError:
        return None


def item_prompt(t):
    return (f'[Дежурный, новый поток] План: `{t["plan"]}`. Ты свежий поток без истории: всё, что нужно, — в плане. '
            'Прочитай шапку плана до первого пункта и раздел «Экономия», затем возьми ПЕРВЫЙ пункт `- [ ]` сверху и '
            'сделай только его; читай только то, что названо в его строке «Читать». '
            f'{MODE_RULES.get(t.get("mode"), MODE_RULES["review"])} {HARD_STOPS} '
            'По ходу пиши под пунктом строку «  - ход: …» (что сделано, что дальше), чтобы следующий поток продолжил, '
            'если тебя оборвёт лимит. '
            f'Пункт закрыт → отметь `- [x]` с итогом одной строкой, закоммить и закончи ответ строкой {ITEM_DONE}. '
            'Пункт нельзя закрыть без решения Дамира, но следующие пункты от него не зависят → запиши вопрос в «На решение Дамиру», '
            f'поменяй отметку пункта на `- [?]` и закончи строкой {ITEM_QUESTION}: дежурство пойдёт дальше, Дамир ответит позже. '
            f'Дальше вообще нельзя (стоп-точка: прод, деньги, текст наружу, удаление) → закончи строкой {NEED_DAMIR}. '
            'В обоих случаях с вопросом сам вопрос напиши в последнем ответе: 1–3 предложения по-русски простыми словами, '
            'твоя рекомендация, номер пункта — этот текст уйдёт Дамиру в чат вопросов. '
            f'Это последний пункт («весь план пройден»), а в плане остались `- [?]` → не закрывай его, закончи строкой {NEED_DAMIR}. '
            f'Открытых пунктов не осталось → закончи строкой {DONE_MARK}.')


def pid_alive(pid):
    if not pid:
        return False
    try:
        done, _ = os.waitpid(pid, os.WNOHANG)  # reap it if it's our child, or it hangs around as a zombie
        if done == pid:
            return False
    except ChildProcessError:
        pass  # started by an earlier daemon: not ours to wait for
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def start_run(key, t, seat='3'):
    RUNS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime('%m%d-%H%M%S')
    n = t.get('runs', 0) + 1  # in the name too: a limited run and the next one can start within the same second
    out, last = RUNS / f'{key}-{stamp}-{n}-s{seat}.log', RUNS / f'{key}-{stamp}-{n}-s{seat}.last.txt'
    cwd = t.get('cwd') or str(Path(t['plan']).parent)
    if seat == '3':
        cmd = [CODEX, 'exec', '--dangerously-bypass-approvals-and-sandbox', '--skip-git-repo-check',
               '-C', cwd, '-o', str(last), item_prompt(t)]
        with open(out, 'w') as f:
            p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=f, stderr=subprocess.STDOUT, start_new_session=True)
    else:
        # claude -p prints only the final answer on stdout, so that's the .last file; the rest goes to the log.
        # Account 1 is the default login: no CLAUDE_CONFIG_DIR at all (with it set to ~/.claude it says "Not logged in", 06.11)
        env = {k: v for k, v in TOOL_ENV.items() if k != 'CLAUDE_CONFIG_DIR'}
        if claude_dir(seat) != HOME / '.claude':
            env['CLAUDE_CONFIG_DIR'] = str(claude_dir(seat))
        cmd = [str(CLAUDE_BIN), '-p', item_prompt(t), '--dangerously-skip-permissions']
        with open(out, 'w') as f, open(last, 'w') as fl:
            p = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=fl, stderr=f, start_new_session=True)
    t.update(run_pid=p.pid, run_log=str(out), run_last=str(last), run_started=time.time(), seat=seat,
             open_before=open_items(t['plan']), runs=t.get('runs', 0) + 1)
    log(f'{key} run {t["runs"]} started on {SEAT_NAMES[seat]}, pid {p.pid}, open items {t["open_before"]}')


def run_outcome(t):
    """How the finished run ended: closed / item / question / damir / limited / broken / failed."""
    try:
        last = Path(t['run_last']).read_text()
    except (OSError, KeyError):
        last = ''
    try:
        with open(t['run_log'], 'rb') as f:
            f.seek(max(0, os.path.getsize(t['run_log']) - 20000))
            tail = f.read().decode('utf-8', 'replace').lower()
    except (OSError, KeyError):
        tail = ''
    if DONE_MARK in last:
        return 'closed'
    if ITEM_QUESTION in last:
        return 'question'
    if NEED_DAMIR in last:
        return 'damir'
    if ITEM_DONE in last:
        return 'item'
    import re
    if (t.get('seat') or '3') == '3':
        return 'limited' if 'usage limit' in tail or 'usage_limit' in tail else 'failed'
    # claude -p: a limit or a dead login is the whole, short answer ("Claude AI usage limit reached",
    # "You've hit your limit · resets 10pm", "Not logged in · Please run /login"). A long answer is real work
    # that may well talk about some API's rate limit or an expired oauth token, so it doesn't count
    short = (last if len(last) < 1000 else '').lower() + '\n' + tail[-2000:]
    if re.search(r'usage limit|limit reached|hit your .{0,12}limit|out of (extra )?usage', short):
        return 'limited'
    if re.search(r'not logged in|please run /login|invalid api key|oauth token (has )?expired', short):
        return 'broken'
    return 'failed'


def tick_items(key, t, now, limits_ok):
    """One step for an items entry. limits_ok() reads limits (no tokens) only when a run is about to start."""
    name = t.get('name') or key
    if pid_alive(t.get('run_pid')):
        t['last_state'] = 'running'
        return
    if t.get('run_pid'):  # a run just finished
        outcome = run_outcome(t)
        opened = open_items(t['plan'])
        progressed = opened is not None and t.get('open_before') is not None and opened < t['open_before']
        t.update(run_pid=None, last_outcome=outcome, state_at=now)
        log(f'{key} run {t.get("runs")} ended: {outcome}, open items {t.get("open_before")} -> {opened}')
        t['no_progress'] = 0 if progressed else t.get('no_progress', 0) + (outcome in ('item', 'failed', 'question'))
        t['failures'] = t.get('failures', 0) + 1 if outcome == 'failed' else 0
        if outcome == 'closed':
            t['done'], t['last_state'] = True, 'done'
            notify('Дежурный: план закрыт', name)
            return
        if outcome in ('question', 'damir'):
            ask_damir(key, t, outcome)
        if outcome == 'damir':
            t['waiting'], t['last_state'] = True, 'waiting'
            return
        if t['failures'] >= MAX_FAILURES or t['no_progress'] >= MAX_NO_PROGRESS:
            t['paused'], t['last_state'] = True, 'stuck'
            log(f'{key} paused: failures {t["failures"]}, no progress {t["no_progress"]}')
            notify('Дежурный: застрял', f'{name} — поставил на паузу, глянь журнал')
            return
        if outcome in ('limited', 'broken'):
            rest_seat(t, t.get('seat') or '3', outcome, now)  # and straight on to the next seat below
    if t.get('waiting'):
        return
    if open_items(t['plan']) == 0:
        if open_items(t['plan'], '?'):
            t['waiting'], t['last_state'] = True, 'waiting'  # only parked items left: nothing to do until he answers
            return
        t['done'], t['last_state'] = True, 'done'
        notify('Дежурный: план закрыт', name)
        return
    seat = pick_seat(t, now, limits_ok)
    if not seat:
        if t.get('last_state') != 'limited':
            notify('Дежурный: лимит', f'{name} — все включённые места в лимите, жду сброса')
        t['last_state'] = 'limited'
        return
    start_run(key, t, seat)
    t['last_state'] = 'running'


def questions_thread():
    return read_watch().get('questions_thread')


# the owner's own skill if there is one next to the code, else the example from the repo
SKILL_FILE = next((d / 'SKILL.md' for d in (BASE / 'skill', BASE / 'skill-example') if (d / 'SKILL.md').exists()),
                  BASE / 'skill-example/SKILL.md')


def ask_damir(key, t, outcome):
    """The run's question goes to Damir's "Дежурный: вопросы" chat in the Codex app; he answers there."""
    try:
        question = Path(t['run_last']).read_text().replace(ITEM_QUESTION, '').replace(NEED_DAMIR, '').strip()
    except (OSError, KeyError):
        question = '(запуск не оставил текста вопроса — смотри «На решение Дамиру» в плане)'
    stop = 'дежурство СТОИТ и ждёт ответа' if outcome == 'damir' else 'дежурство идёт дальше, пункт отложен с отметкой `- [?]`'
    msg = (f'[Дежурный · вопрос] {t.get("name") or key} ({stop}).\n\n{question}\n\n'
           f'Для тебя, Codex: перескажи Дамиру этот вопрос коротко по-русски и жди его ответа, сам ничего не решай. '
           f'Когда ответит — сделай по разделу «Чат вопросов» в `{SKILL_FILE}` (прочитай его): '
           f'план `{t["plan"]}`, запись дежурства `{key}`.')
    qid = questions_thread()
    sent = False
    if qid:
        sent = queue_message(qid, msg)
        if sent and not loaded_in_app(qid):
            open_in_app(qid)
    log(f'{key} question ({outcome}) -> {"chat " + qid if sent else "no chat, notification only"}')
    notify('Дежурный: вопрос', f'{t.get("name") or key} — ответь в чате «Дежурный: вопросы»' if sent
           else f'{t.get("name") or key} — вопрос в плане, чат вопросов не задан')


# ---------- daemon ----------

class Caffeine:
    proc = None

    def hold(self, on):
        if on and not (self.proc and self.proc.poll() is None):
            self.proc = subprocess.Popen(['caffeinate', '-ims', '-w', str(os.getpid())])
            log('caffeinate on')
        elif not on and self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self.proc = None
            log('caffeinate off')


def should_hold(live):
    """Mac mode decides; the battery floor beats any mode."""
    m = read_mac_mode()
    if m.get('expired'):
        set_mac_mode('normal')
    want = m['mode'] == 'night' or (m['mode'] == 'duty' and live)
    if want:
        on_batt, pct = battery()
        if on_batt and pct is not None and pct < BATTERY_FLOOR:
            return False
    return want


def tick(caf, limits_box):
    now = time.time()
    with watch_list() as data:
        threads = data['threads']
        live = {k: v for k, v in threads.items() if not v.get('paused') and not v.get('done')}
        caf.hold(should_hold(bool(live)))

        def limits_ok():
            lim = limits_box.get('v')
            reset = next_reset(lim) if lim else None
            if not lim or now - lim['checked_at'] > LIMITS_EVERY or (reset and now > reset + AFTER_RESET
                                                                      and lim['checked_at'] < reset + AFTER_RESET):
                try:
                    lim = limits_box['v'] = read_limits()
                except Exception as e:
                    log(f'limits read failed: {e}')
                    return False
            return lim['allowed']

        for key, t in list(live.items()):
            if t.get('kind') == 'items':
                prev = t.get('last_state')
                tick_items(key, t, now, limits_ok)
                if t.get('last_state') != prev:
                    log(f'{key} {prev} -> {t.get("last_state")}')
                    limits_box.pop('v', None) if t.get('last_state') in ('running', 'limited') else None
                live.pop(key)
        need_limits = False
        for tid, t in live.items():
            st = thread_state(tid)
            prev = t.get('last_state')
            t['last_state'], t['state_at'] = st['state'], st.get('at')
            name = t.get('name') or tid[:8]
            if st['state'] != prev:
                log(f'{tid} {prev} -> {st["state"]}')
            if st['state'] == 'done':
                t['done'] = True
                notify('Дежурный: план закрыт', name)
                log(f'{tid} plan closed, off duty')
            elif st['state'] == 'limited':
                t.pop('wait_notified', None)
                if (t.get('last_wake_at') or 0) < st['at']:
                    need_limits = True
                    if prev != 'limited':
                        notify('Дежурный: лимит', f'{name} — жду сброса')
            elif st['state'] == 'running':
                t.pop('wait_notified', None)
                t.pop('wake_failed_at', None)
            elif st['state'] == 'idle':
                woke = t.get('last_wake_at') or 0
                if woke and st['at'] < woke and now - woke > WAKE_CONFIRM and not t.get('wake_failed_at'):
                    t['wake_failed_at'] = now
                    log(f'{tid} woke but no new turn in {WAKE_CONFIRM}s')
                    notify('Дежурный: не проснулся', f'{name} — открой поток в Codex')
                elif st['at'] >= woke and now - st['at'] > WAIT_NOTICE and t.get('wait_notified') != st['at']:
                    t['wait_notified'] = st['at']
                    notify('Дежурный: ждёт тебя', f'{name}: {(st.get("msg") or "").strip()[-120:]}')
                    log(f'{tid} idle, waiting for Damir')
            elif st['state'] == 'missing':
                log(f'{tid} rollout not found')

        if not need_limits:
            return
        lim = limits_box.get('v')
        reset = next_reset(lim) if lim else None
        stale = not lim or now - lim['checked_at'] > LIMITS_EVERY or (reset and now > reset + AFTER_RESET
                                                                       and lim['checked_at'] < reset + AFTER_RESET)
        if stale:
            try:
                lim = limits_box['v'] = read_limits()
            except Exception as e:
                log(f'limits read failed: {e}')
                return
        if not lim['allowed']:
            return
        for tid, t in live.items():
            woke = t.get('last_wake_at') or 0
            # a wake that ran straight into the limit again must not turn into a wake every minute
            if t.get('last_state') == 'limited' and woke < (t.get('state_at') or 0) and now - woke > LIMITS_EVERY:
                limits_box.pop('v', None)  # whatever happens next, decide on fresh numbers
                if wake(tid, t):
                    t['last_wake_at'] = time.time()
                    t['wakes'] = t.get('wakes', 0) + 1
                    t.pop('wake_failed_at', None)
                    notify('Дежурный: разбудил', t.get('name') or tid[:8])


def daemon():
    log(f'daemon start pid {os.getpid()}')
    caf, box = Caffeine(), {}
    signal.signal(signal.SIGTERM, lambda *_: (caf.hold(False), sys.exit(0)))
    while True:
        try:
            tick(caf, box)
        except Exception as e:
            log(f'tick error: {e!r}')
        # a flipped mac mode should bite in seconds, not after a whole tick
        seen = MAC_MODE.stat().st_mtime if MAC_MODE.exists() else 0
        for _ in range(TICK // 5):
            time.sleep(5)
            if (MAC_MODE.stat().st_mtime if MAC_MODE.exists() else 0) != seen:
                break


# ---------- CLI ----------

def resolve(data, key):
    if key in data['threads']:
        return key
    hits = [k for k, v in data['threads'].items() if k.startswith(key) or (v.get('name') or '').lower() == key.lower()]
    if len(hits) != 1:
        sys.exit(f'не нашёл поток «{key}» в списке дежурства')
    return hits[0]


def main():
    ap = argparse.ArgumentParser(description='Дежурный Codex')
    sub = ap.add_subparsers(dest='cmd', required=True)
    a = sub.add_parser('add'); a.add_argument('thread', nargs='?', default=os.environ.get('CODEX_THREAD_ID'))
    a.add_argument('--mode', choices=list(MODE_RULES), default='review'); a.add_argument('--plan'); a.add_argument('--name')
    ai = sub.add_parser('add-items', help='plan run item by item, a fresh codex exec per item')
    ai.add_argument('--plan', required=True); ai.add_argument('--name'); ai.add_argument('--cwd')
    ai.add_argument('--mode', choices=list(MODE_RULES), default='review')
    qc = sub.add_parser('questions-chat', help='this Codex chat gets the questions of item runs')
    qc.add_argument('thread', nargs='?', default=os.environ.get('CODEX_THREAD_ID'))
    sub.add_parser('test-notify')
    for c in ('remove', 'pause', 'resume'):
        sub.add_parser(c).add_argument('thread')
    sub.add_parser('list'); s = sub.add_parser('status'); s.add_argument('--json', action='store_true')
    sub.add_parser('limits')
    w = sub.add_parser('wake'); w.add_argument('thread'); w.add_argument('--force', action='store_true')
    sub.add_parser('daemon'); sub.add_parser('on'); sub.add_parser('off'); sub.add_parser('tokens')
    mm = sub.add_parser('mac-mode', help='normal: never keep the mac awake; duty: while a duty is live; night: till 08:00')
    mm.add_argument('mode', choices=MAC_MODES)
    cs = sub.add_parser('carousel-start', help='carousel of accounts for the claude session with this id (from /status)')
    cs.add_argument('sid')
    cx = sub.add_parser('carousel-stop'); cx.add_argument('sid')
    se = sub.add_parser('seat', help='1 = Claude 1, 2 = Claude 2, 3 = Codex: may item runs and the carousel use it')
    se.add_argument('n', choices=SEATS); se.add_argument('state', choices=('on', 'off'))
    args = ap.parse_args()

    if args.cmd == 'daemon':
        return daemon()
    if args.cmd in ('on', 'off'):
        daemon_switch(args.cmd == 'on')
        print(f'сторож {"включён" if args.cmd == "on" else "выключен"}')
        return
    if args.cmd == 'tokens':
        try:
            print(json.dumps(count_tokens(), indent=2))
        finally:
            TOKENS_LOCK.unlink(missing_ok=True)
        return
    if args.cmd == 'carousel-start':
        carousel_start(args.sid)
        return
    if args.cmd == 'carousel-stop':
        carousel_stop(args.sid)
        return
    if args.cmd == 'seat':
        set_seat(args.n, args.state == 'on')
        print(f'{SEAT_NAMES[args.n]}: {"включён" if args.state == "on" else "выключен"}')
        return
    if args.cmd == 'mac-mode':
        set_mac_mode(args.mode)
        return
    if args.cmd == 'limits':
        print(json.dumps(read_limits(), indent=2, ensure_ascii=False))
        return
    if args.cmd == 'test-notify':
        notify('Дежурный: проверка', 'Если видишь это — уведомления работают')
        print('отправил')
        return
    if args.cmd == 'questions-chat':
        if not args.thread or not rollout_path(args.thread):
            sys.exit('нет такого потока: запусти из чата Codex (CODEX_THREAD_ID) или передай id')
        with watch_list() as data:
            data['questions_thread'] = args.thread
        log(f'questions chat = {args.thread}')
        print(f'Чат вопросов дежурного: {args.thread}')
        return
    if args.cmd == 'add-items':
        plan = str(Path(args.plan).expanduser().resolve())
        if open_items(plan) is None:
            sys.exit(f'план {plan} не читается')
        key = 'items-' + Path(plan).stem
        with watch_list() as data:
            old = data['threads'].get(key, {})
            data['threads'][key] = {**old, 'kind': 'items', 'name': args.name or old.get('name') or Path(plan).stem,
                                    'mode': args.mode, 'plan': plan, 'cwd': args.cwd or old.get('cwd') or str(Path(plan).parent),
                                    'added_at': old.get('added_at') or time.time(), 'paused': False, 'done': False,
                                    'waiting': False, 'failures': 0, 'no_progress': 0}
        log(f'{key} on duty item by item, mode {args.mode}, plan {plan}')
        print(f'{key} на дежурстве по пунктам: режим {args.mode}, план {plan}, открытых пунктов {open_items(plan)}.')
        return
    if args.cmd == 'add':
        if not args.thread:
            sys.exit('нет id потока: передай его или запусти из Codex (CODEX_THREAD_ID)')
        if not rollout_path(args.thread):
            sys.exit(f'поток {args.thread} не найден в ~/.codex/sessions')
        meta = thread_meta(args.thread)
        with watch_list() as data:
            old = data['threads'].get(args.thread, {})
            data['threads'][args.thread] = {**old, 'name': args.name or old.get('name') or Path(meta.get('cwd') or '').name,
                                            'mode': args.mode, 'plan': args.plan or old.get('plan'),
                                            'cwd': meta.get('cwd'), 'added_at': old.get('added_at') or time.time(),
                                            'paused': False, 'done': False}
        log(f'{args.thread} on duty, mode {args.mode}, plan {args.plan}')
        print(f'Поток {args.thread} на дежурстве: режим {args.mode}, план {args.plan or "—"}.')
        return
    if args.cmd in ('remove', 'pause', 'resume'):
        with watch_list() as data:
            keys = list(data['threads']) if args.thread == 'all' else [resolve(data, args.thread)]
            for k in keys:
                if args.cmd == 'remove':
                    data['threads'].pop(k)
                else:
                    data['threads'][k]['paused'] = args.cmd == 'pause'
                    if args.cmd == 'resume':
                        data['threads'][k].update(done=False, waiting=False, failures=0, no_progress=0)
        log(f'{args.cmd} {", ".join(keys)}')
        print(f'{args.cmd}: {", ".join(keys)}')
        return
    if args.cmd == 'wake':
        data = read_watch()
        tid = resolve(data, args.thread)
        if data['threads'][tid].get('kind') == 'items':
            sys.exit('это дежурство по пунктам: новый запуск сторож делает сам, будить нечего')
        st = thread_state(tid)
        if st['state'] == 'running':
            sys.exit('поток сейчас работает — не трогаю')
        if st['state'] != 'limited' and not args.force:
            sys.exit(f'поток не на лимите ({st["state"]}); --force, чтобы всё равно разбудить')
        if wake(tid, data['threads'][tid]):
            with watch_list() as d:
                d['threads'][tid]['last_wake_at'] = time.time()
                d['threads'][tid]['wakes'] = d['threads'][tid].get('wakes', 0) + 1
            print('разбудил')
        return
    # list / status
    data = read_watch()
    try:
        lim = json.loads(LIMITS_CACHE.read_text())
    except (OSError, ValueError):
        lim = None
    rows = []
    for tid, t in data['threads'].items():
        if t.get('kind') == 'items':
            # the menu bar knows running/limited/idle/done/paused; a question for Damir is its "idle" (= waits for you)
            state = ('paused' if t.get('paused') else 'done' if t.get('done') else 'idle' if t.get('waiting')
                     else {'stuck': 'paused'}.get(t.get('last_state'), t.get('last_state') or 'idle'))
            rows.append({'id': tid, 'name': t.get('name'), 'mode': t.get('mode'), 'plan': t.get('plan'), 'state': state,
                         'seat': SEAT_NAMES.get(t.get('seat') or '3'),
                         'state_at': t.get('state_at') or t.get('run_started'), 'wakes': t.get('runs', 0), 'loaded': False,
                         'open_thread': data.get('questions_thread'), 'pending': open_items(t['plan'], '?') or 0,
                         'last_msg': f'запусков {t.get("runs", 0)}, место {SEAT_NAMES.get(t.get("seat") or "3")}, '
                                     f'открытых пунктов {open_items(t["plan"])}, '
                                     f'последний: {t.get("last_outcome") or "—"}, журнал {t.get("run_log") or "—"}'})
            continue
        st = thread_state(tid)
        state = 'paused' if t.get('paused') else 'done' if t.get('done') else st['state']
        rows.append({'id': tid, 'name': t.get('name'), 'mode': t.get('mode'), 'plan': t.get('plan'),
                     'state': state, 'state_at': st.get('at'), 'wakes': t.get('wakes', 0),
                     'loaded': loaded_in_app(tid), 'last_msg': (st.get('msg') or '')[-200:]})
    if getattr(args, 'json', False):
        print(json.dumps({'threads': rows, 'limits': lim, 'claude': claude_accounts(), 'mac': mac_status(),
                          'carousel': carousel_status(), 'tokens': tokens_cached(),
                          'daemon': bool(subprocess.run(['pgrep', '-f', 'codex_wake.py daemon'], capture_output=True).stdout)},
                         ensure_ascii=False, indent=2))
        return
    if lim:
        f = lambda w: f'{w["usedPercent"]}% до {datetime.fromtimestamp(w["resetsAt"]):%d.%m %H:%M}' if w else '—'
        print(f'Лимиты: 5ч {f(lim["primary"])}, неделя {f(lim["secondary"])}, бесплатных сбросов {lim.get("free_resets")}')
    for r in rows:
        print(f'{r["state"]:8} {r["name"]:30} {r["mode"]:11} будил {r["wakes"]}×  {r["id"]}'
              + (f'  · {r["seat"]}' if r.get('seat') else ''))
    if not rows:
        print('На дежурстве никого.')


if __name__ == '__main__':
    main()
