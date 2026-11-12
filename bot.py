"""Local, visible-browser poll watcher. Stop with Ctrl+C."""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import time
from urllib.parse import urlsplit, urlunsplit

try:
    from playwright.sync_api import Error as BrowserError, sync_playwright
except ModuleNotFoundError as error:
    if error.name != 'playwright':
        raise
    sync_playwright = None

    class BrowserError(Exception):
        """Allows dependency-free tests of the watcher state machine."""

        @property
        def message(self):
            return str(self)

LOG = logging.getLogger("pollev")
BASE = Path(__file__).resolve().parent
SELECTORS = {
    "root": "main, [role='main']",
    "question": "h1, h2, [data-question]",
    "options": "input[type='radio'], [role='radio'], [data-choice-id], [data-response-option], [data-activity-type='multiple_choice_poll'] button.component-response-multiple-choice__option__vote",
    "submit": "button[type='submit'], input[type='submit']",
    "confirmation": "[data-response-submitted='true'], [role='status']",
    "closed": "[data-poll-closed='true']",
}

# Read all relevant state in one evaluation; never guess that an arbitrary button
# is an answer. Selectors include the observed Poll Everywhere vote buttons.
SNAPSHOT = r"""cfg => {
    const visible = e => !!e && [...e.getClientRects()].some(r => r.width > 0 && r.height > 0) &&
        getComputedStyle(e).visibility !== 'hidden';
    const roots = [...document.querySelectorAll(cfg.root)].filter(visible);
    if (roots.length !== 1) return {status: 'waiting', reason: 'No unique poll container'};
    const root = roots[0];
    const text = root.innerText || '';
    const any = selector => [...root.querySelectorAll(selector)].some(visible);
    if (any(cfg.closed) || /poll is closed|not accepting responses|no active (poll|question|activity)/i.test(text))
        return {status: 'waiting', reason: 'No open poll'};
    if (/sign in to respond|log in to respond|verify your email/i.test(text))
        return {status: 'waiting', reason: 'Sign-in required'};
    const legacy = root.querySelector('[data-activity-type="multiple_choice_poll"]');
    if (any("input[type=checkbox], [role=checkbox]") ||
        (!legacy && /rank the|rank these|select all that apply/i.test(text)))
        return {status: 'waiting', reason: 'Unsupported poll type'};
    const nodes = [...root.querySelectorAll(cfg.options)];
    const usable = nodes.map((e, index) => {
        const label = e.labels && [...e.labels].find(visible);
        const target = visible(e) ? e : label;
        if (!target) return null;
        const value = e.querySelector('.component-response-multiple-choice__option__value');
        const voted = Number(e.closest('.component-response-multiple-choice__option')?.getAttribute('data-vote-count') || 0) > 0;
        // Vote counts and accessible labels change after voting; answer text must
        // remain stable so the fallback identity does not change with them.
        const name = (value && value.textContent) || e.getAttribute('aria-label') || (label && label.innerText) ||
            e.innerText || e.getAttribute('value') || '';
        return {index, text: name.trim(), disabled: e.disabled ||
            e.getAttribute('aria-disabled') === 'true',
            selected: e.checked || e.getAttribute('aria-checked') === 'true' ||
                e.getAttribute('aria-pressed') === 'true' || voted};
    }).filter(Boolean);
    if (!usable.length || usable.some(o => !o.text))
        return {status: 'waiting', reason: 'No recognizable multiple-choice answers'};
    // Native radio + its wrapper must not be counted twice.
    if (nodes.some(e => nodes.some(other => e !== other && e.contains(other))))
        return {status: 'waiting', reason: 'Overlapping answer controls; unsupported page layout'};
    const q = (legacy && legacy.querySelector('.component-response-header__title')) ||
        [...root.querySelectorAll(cfg.question)].find(visible);
    if (!q || !q.innerText.trim()) return {status: 'waiting', reason: 'No question identifier'};
    const idNode = root.closest('[data-poll-id], [data-activity-id]') ||
        root.querySelector('[data-poll-id], [data-activity-id]');
    let id = idNode && (idNode.getAttribute('data-poll-id') || idNode.getAttribute('data-activity-id'));
    if (!id && legacy) {
        const frame = legacy.closest('turbo-frame[src]');
        const path = frame && new URL(frame.getAttribute('src'), location.href).pathname;
        if (path && /^\/multiple_choice_polls\/[^/]+\/respond\/?$/.test(path)) id = 'pollev:' + path;
    }
    const confirmed = (legacy && usable.some(o => o.selected)) ||
        [...root.querySelectorAll(cfg.confirmation)].some(e => visible(e) &&
        (e.getAttribute('data-response-submitted') === 'true' ||
        /response (received|submitted|recorded)|thank you.*respond/i.test(e.innerText)));
    const locked = legacy && /this poll is locked|poll is closed|not accepting responses/i.test(
        legacy.querySelector('.component-response-header__status')?.innerText || '');
    return {status: 'poll', id, question: q.innerText.trim(), legacy: !!legacy, confirmed: !!confirmed,
        options: usable, answered: confirmed || usable.some(o => o.selected),
        locked: !!locked, disabled: !!locked || usable[0].disabled};
}"""

# The legacy responseView's button handler adds a result then triggers 'result'
# on its view. The view's listener marks it pending and saves it. Calling that
# same path directly permits concurrent requests; further DOM clicks are blocked
# while ANY result is pending, even on polls allowing multiple responses.
COUNT_BURST = r"""({cfg, expected, limit}) => {
    const snapshot = (""" + SNAPSHOT + r""");
    const identity = p => JSON.stringify(p.id ? ['id', p.id] :
        ['content', p.question, p.options.map(o => o.text)]);
    const wanted = identity(expected);
    let attempted = 0;
    let transport = 'click';
    const started = performance.now();
    while (attempted < limit) {
        const current = snapshot(cfg);
        if (current.status !== 'poll' || !current.legacy || current.locked || identity(current) !== wanted) break;
        const root = [...document.querySelectorAll(cfg.root)].find(e =>
            [...e.getClientRects()].some(r => r.width > 0 && r.height > 0) &&
            getComputedStyle(e).visibility !== 'hidden');
        const button = root?.querySelectorAll(cfg.options)[current.options[0].index];
        if (!button?.isConnected) break;
        const response = button.closest('legacy-response-view')?.responseView;
        const model = response?.model;
        const option = model?.mcpOptions?.at?.(current.options[0].index);
        if (model?.type?.() === 'multiple_choice_poll' && option?.id != null &&
            typeof model.isStarted === 'function' && typeof model.isWebEnabled === 'function' &&
            typeof model.results?.add === 'function' && typeof model.results?.filter === 'function' &&
            typeof response.view?.trigger === 'function') {
            transport = 'app';
            // Respect actual poll limits. Only the pending-request UI guard is
            // bypassed; closed polls, total caps and per-option caps still stop.
            if (!model.isStarted() || !model.isWebEnabled() || model.isMaxedOut?.()) break;
            const maximum = Number(model.get('max_votes') || 0);
            const perOption = Number(model.get('responses_per_option') ||
                model.get('type_options')?.responses_per_option || 0);
            if (maximum && model.results.length >= maximum) break;
            if (perOption && model.results.filter(r => r.get('option_id') === option.id).length >= perOption) break;
            const pending = model.results.filter(r => r.has('isPending')).length > 0;
            if (current.disabled && !pending) break;
            attempted++;
            const result = model.results.add({option_id: option.id});
            response.view.trigger('result', result, option);
        } else {
            if (current.disabled || button.disabled || button.getAttribute('aria-disabled') === 'true') break;
            attempted++;
            button.click();
        }
    }
    return {attempted, elapsed_ms: performance.now() - started, transport};
}"""


def poll_key(url: str, poll: dict) -> str:
    identity = (['id', poll['id']] if poll.get('id') else
                ['content', poll['question'], [o['text'] for o in poll['options']]])
    return hashlib.sha256(json.dumps([url, identity], ensure_ascii=False).encode()).hexdigest()


class History:
    """Durable at-most-once attempts, including uncertain submissions."""

    def __init__(self, path: Path):
        self.path = path
        self.entries = json.loads(path.read_text()) if path.exists() else {}
        if not isinstance(self.entries, dict) or any(
            value not in ('attempted', 'confirmed', 'already-answered')
            for value in self.entries.values()
        ):
            raise ValueError('Invalid history file; refusing to risk duplicate votes')

    def record(self, key: str, status: str) -> None:
        updated = {**self.entries, key: status}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix('.tmp')
        with temp.open('w') as stream:
            json.dump(updated, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(self.path)
        self.entries = updated


class Watcher:
    def __init__(self, page, url: str, history: History, repeat=False, count=None, count_batches=None):
        self.page, self.url, self.history = page, url, history
        self.repeat = repeat
        self.batch_size = count_batches[0] if count_batches is not None else None
        self.batches = count_batches[1] if count_batches is not None else None
        self.count = self.batch_size * self.batches if count_batches is not None else count
        self.count_attempts = {}
        self.reported = set()
        self.last_reason = None
        self.attempted = self.confirmed = 0

    def snapshot(self):
        return self.page.evaluate(SNAPSHOT, SELECTORS)

    def tick(self):
        if urlsplit(self.page.url).hostname != urlsplit(self.url).hostname:
            if self.last_reason != 'Sign-in or navigation outside participant site':
                LOG.info('Sign-in or navigation outside participant site; waiting')
                self.last_reason = 'Sign-in or navigation outside participant site'
            return
        poll = self.snapshot()
        if poll['status'] != 'poll':
            if poll['reason'] != self.last_reason:
                LOG.info('%s; monitoring continues', poll['reason'])
                self.last_reason = poll['reason']
            return
        self.last_reason = None
        key = poll_key(self.url, poll)
        multiple = self.repeat or self.count is not None
        attempt_number = None
        if self.count is not None:
            # A new run is an explicit request for a fresh batch. Retain the
            # per-poll cap across page reloads and revisits within this run.
            attempt_number = self.count_attempts.get(key, 0) + 1
            if attempt_number > self.count:
                return
        if not multiple and key in self.history.entries:
            return
        if not multiple and poll['answered']:
            self.history.record(key, 'already-answered')
            if key not in self.reported:
                LOG.info('Skipping already-answered poll')
                self.reported.add(key)
            return
        if self.count is not None and poll.get('legacy'):
            # Batch mode can continue while earlier requests are pending. The
            # browser handler still checks locks and actual response limits.
            if poll.get('locked') or (poll['disabled'] and self.batch_size is None):
                return
            self.count_burst(key, poll)
            return
        if poll['disabled']:
            return
        # Resolve a concrete DOM handle and recheck the poll before interaction.
        root = self.page.locator(SELECTORS['root']).filter(visible=True)
        option = root.locator(SELECTORS['options']).nth(poll['options'][0]['index'])
        handle = option.element_handle()
        fresh = self.snapshot()
        if (not handle or fresh['status'] != 'poll' or poll_key(self.url, fresh) != key
                or (fresh['answered'] and not multiple) or fresh['disabled']):
            return
        # Reserve BEFORE the click: a timeout may follow an accepted vote.
        # Default mode persists its guard; count mode reserves a session slot.
        if key not in self.history.entries:
            self.history.record(key, 'attempted')
        if attempt_number is not None:
            self.count_attempts[key] = attempt_number
        self.attempted += 1
        if handle.evaluate("e => e.tagName === 'INPUT' && e.type === 'radio'"):
            if handle.is_visible():
                if multiple:
                    handle.click(timeout=3000)
                else:
                    handle.check(timeout=3000)
            else:
                label = handle.evaluate_handle("e => [...e.labels].find(l => l.getClientRects().length)").as_element()
                if label is None:
                    raise BrowserError('Hidden answer has no clickable label; attempt will not be retried')
                label.click(timeout=3000)
        else:
            handle.click(timeout=3000)
        after = self.snapshot()
        if after['status'] == 'poll' and poll_key(self.url, after) == key and not after.get('legacy'):
            submit = root.locator(SELECTORS['submit']).filter(visible=True)
            if not submit.count():
                submit = root.get_by_role('button', name=re.compile(r'^(submit|send|vote)( response| answer)?$', re.I)).filter(visible=True)
            if submit.count() == 1 and submit.is_enabled():
                submit_handle = submit.element_handle()
                current = self.snapshot()
                if submit_handle and current['status'] == 'poll' and poll_key(self.url, current) == key:
                    submit_handle.click(timeout=3000)
        if multiple:
            # Don't serialize repeated clicks behind a three-second confirmation
            # wait. A click is an attempt, not proof of a distinct server response.
            if self.count is not None:
                LOG.info('First-answer attempt %s/%d; server acceptance unconfirmed',
                         attempt_number, self.count)
                if attempt_number == self.count:
                    LOG.info('Count complete for this poll; waiting for a new poll')
            return
        # Checked radios alone are not proof the server accepted the response.
        try:
            self.page.wait_for_function(
                'cfg => { const poll = (' + SNAPSHOT + ')(cfg); '
                "return poll.status === 'poll' && poll.confirmed; }",
                arg=SELECTORS, timeout=3000)
            current = self.snapshot()
            if current['status'] == 'poll' and poll_key(self.url, current) == key:
                self.history.record(key, 'confirmed')
                self.confirmed += 1
                LOG.info('Submission confirmed')
                return
        except BrowserError:
            pass
        LOG.info('First-answer attempt complete; acceptance unconfirmed, will not retry')

    def count_burst(self, key, poll):
        used = self.count_attempts.get(key, 0)
        remaining = self.count - used
        if self.batch_size is not None:
            remaining = min(remaining, self.batch_size - used % self.batch_size)
        if key not in self.history.entries:
            self.history.record(key, 'attempted')
        # If the browser call fails after dispatch, do not repeat an uncertain
        # batch. A successful result releases slots that were never clicked.
        self.count_attempts[key] = used + remaining
        self.attempted += remaining
        started = time.perf_counter()
        result = self.page.evaluate(COUNT_BURST, {
            'cfg': SELECTORS, 'expected': poll, 'limit': remaining,
        })
        sent = result['attempted']
        self.count_attempts[key] = used + sent
        self.attempted -= remaining - sent
        LOG.info('First-answer burst via %s: %d attempts in %.1f ms (browser %.1f ms); '
                 '%d/%d this run; server acceptance unconfirmed',
                 result['transport'], sent, (time.perf_counter() - started) * 1000,
                 result['elapsed_ms'], used + sent, self.count)
        if self.batch_size is not None and sent:
            LOG.info('Batch %d/%d: %d/%d attempts dispatched',
                     used // self.batch_size + 1, self.batches,
                     used % self.batch_size + sent, self.batch_size)
        if used + sent == self.count:
            LOG.info('Count complete for this poll; waiting for a new poll')


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True, help='Poll Everywhere participant URL')
    parser.add_argument('--interval', type=float, default=2, help='Seconds between checks (default: 2)')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--repeat', nargs='?', type=int, const=20, default=None,
                      metavar='N', help='Repeat first-answer attempts N times per second (default: 20) until Ctrl+C')
    mode.add_argument('--count', type=int, default=None, metavar='N',
                      help='Attempt the first answer N times per poll per run with no added delay')
    mode.add_argument('--count_batches', type=int, nargs=2, default=None,
                      metavar=('SIZE', 'BATCHES'),
                      help='Send SIZE first-answer attempts at once, BATCHES times per poll per run, with no added delay')
    args = parser.parse_args(argv)
    parts = urlsplit(args.url)
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
        parser.error('--url must be an http(s) URL without embedded credentials')
    args.url = urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip('/'), parts.query, ''))
    if not math.isfinite(args.interval) or args.interval < 0.5:
        parser.error('--interval must be finite and at least 0.5 seconds')
    if args.repeat is not None and args.repeat < 1:
        parser.error('--repeat N must be a positive integer')
    if args.count is not None and args.count < 1:
        parser.error('--count N must be a positive integer')
    if args.count_batches is not None and any(value < 1 for value in args.count_batches):
        parser.error('--count_batches SIZE BATCHES requires two positive integers')
    return args


def next_repeat_slot(previous: float, now: float, period: float) -> float:
    """Keep a steady rate and discard missed slots instead of sending bursts."""
    target = previous + period
    if target < now:
        target += math.ceil((now - target) / period) * period
    return target


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s', datefmt='%H:%M:%S')
    watcher = None
    try:
        if sync_playwright is None:
            raise ValueError('Playwright is missing. Install it with: python -m pip install -r requirements.txt')
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(BASE / '.local' / 'browser'), headless=False
            )
            page = None
            try:
                history = History(BASE / '.local' / 'history.json')
                context.set_default_timeout(5000)
                page = context.pages[0] if context.pages else context.new_page()
                try:
                    page.goto(args.url, wait_until='domcontentloaded', timeout=30000)
                except BrowserError as error:
                    LOG.warning('Initial navigation incomplete: %s', error.message.splitlines()[0])
                input('Finish login and open the participant page, then press Enter here to monitor: ')
                watcher = Watcher(page, args.url, history, repeat=args.repeat is not None,
                                  count=args.count, count_batches=args.count_batches)
                if args.count_batches is not None:
                    LOG.info('Sending %d batches of %d first-answer attempts per poll (%d total) with no added delay; monitoring until Ctrl+C',
                             watcher.batches, watcher.batch_size, watcher.count)
                elif args.count is not None:
                    LOG.info('Attempting first answer up to %d times per poll with no added delay; monitoring until Ctrl+C',
                             args.count)
                elif args.repeat is None:
                    LOG.info('Monitoring until Ctrl+C')
                else:
                    LOG.info('Repeating first-answer attempts at up to %d/second until Ctrl+C', args.repeat)
                period = 1 / args.repeat if args.repeat is not None else args.interval
                slot = time.monotonic()
                failures = 0
                while not page.is_closed():
                    attempted_before = watcher.attempted
                    try:
                        watcher.tick()
                        failures = 0
                    except BrowserError as error:
                        if page.is_closed():
                            break
                        failures += 1
                        LOG.warning('Browser error%s: %s',
                                    '; pausing repeat attempts' if watcher.repeat else '; any recorded attempt will not be retried',
                                    error.message.splitlines()[0])
                    if watcher.count is not None and not failures and watcher.attempted > attempted_before:
                        # Send the next count attempt immediately. Idle polls and
                        # errors still wait, so monitoring does not busy-loop.
                        continue
                    if args.repeat is not None and not failures:
                        slot = next_repeat_slot(slot, time.monotonic(), period)
                        delay = max(0, slot - time.monotonic())
                    else:
                        delay = min(30, args.interval * 2 ** min(failures, 4))
                        slot = time.monotonic() + delay
                    try:
                        page.wait_for_timeout(delay * 1000)
                    except BrowserError:
                        if page.is_closed():
                            break
                        raise
                    if failures >= 3:
                        # Reload only after errors, not on every tick. Normal live
                        # updates remain under the site's own control.
                        try:
                            page.reload(wait_until='domcontentloaded', timeout=15000)
                        except BrowserError:
                            pass
                LOG.info('Browser closed; monitoring stopped')
            finally:
                try:
                    context.close()
                except BrowserError:
                    if page is None or not page.is_closed():
                        raise
    except (KeyboardInterrupt, EOFError):
        LOG.info('Stopped')
    except (OSError, ValueError, BrowserError) as error:
        LOG.error('%s', error)
        return 1
    finally:
        if watcher:
            if watcher.repeat or watcher.count is not None:
                LOG.info('Session: %d first-answer attempts; server acceptance not counted', watcher.attempted)
            else:
                LOG.info('Session: %d attempts, %d visibly confirmed', watcher.attempted, watcher.confirmed)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
