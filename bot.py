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
DEFAULT_SELECTORS = {
    "root": "main, [role='main']",
    "question": "h1, h2, [data-question]",
    "options": "input[type='radio'], [role='radio'], [data-choice-id], [data-response-option], [data-activity-type='multiple_choice_poll'] button.component-response-multiple-choice__option__vote",
    "submit": "button[type='submit'], input[type='submit']",
    "confirmation": "[data-response-submitted='true'], [role='status']",
    "closed": "[data-poll-closed='true']",
}

# Read all relevant state in one evaluation; never guess that an arbitrary button
# is an answer. Custom selectors support button-based participant pages.
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
        return {status: 'waiting', reason: 'Overlapping answer selectors; refine configuration'};
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
        disabled: !!locked || usable[0].disabled};
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
    def __init__(self, page, url: str, selectors: dict, history: History, dry_run=False):
        self.page, self.url, self.selectors, self.history = page, url, selectors, history
        self.dry_run = dry_run
        self.reported = set()
        self.last_reason = None
        self.attempted = self.confirmed = 0

    def snapshot(self):
        return self.page.evaluate(SNAPSHOT, self.selectors)

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
        if key in self.history.entries:
            return
        if poll['answered']:
            if not self.dry_run:
                self.history.record(key, 'already-answered')
            if key not in self.reported:
                LOG.info('Skipping already-answered poll')
                self.reported.add(key)
            return
        if poll['disabled']:
            return
        if self.dry_run:
            if key not in self.reported:
                LOG.info('Dry run: would select first answer: %s', poll['options'][0]['text'])
                self.reported.add(key)
            return
        # Resolve a concrete DOM handle and recheck the poll before interaction.
        root = self.page.locator(self.selectors['root']).filter(visible=True)
        option = root.locator(self.selectors['options']).nth(poll['options'][0]['index'])
        handle = option.element_handle()
        fresh = self.snapshot()
        if (not handle or fresh['status'] != 'poll' or poll_key(self.url, fresh) != key
                or fresh['answered'] or fresh['disabled']):
            return
        # Persist BEFORE the click. A timeout may occur after the server accepted
        # the vote, so neither reconnection nor restart may automatically retry.
        self.history.record(key, 'attempted')
        self.attempted += 1
        if handle.evaluate("e => e.tagName === 'INPUT' && e.type === 'radio'"):
            if handle.is_visible():
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
            submit = root.locator(self.selectors['submit']).filter(visible=True)
            if not submit.count():
                submit = root.get_by_role('button', name=re.compile(r'^(submit|send|vote)( response| answer)?$', re.I)).filter(visible=True)
            if submit.count() == 1 and submit.is_enabled():
                submit_handle = submit.element_handle()
                current = self.snapshot()
                if submit_handle and current['status'] == 'poll' and poll_key(self.url, current) == key:
                    submit_handle.click(timeout=3000)
        # Checked radios alone are not proof the server accepted the response.
        try:
            self.page.wait_for_function(
                'cfg => { const poll = (' + SNAPSHOT + ')(cfg); '
                "return poll.status === 'poll' && poll.confirmed; }",
                arg=self.selectors, timeout=3000)
            current = self.snapshot()
            if current['status'] == 'poll' and poll_key(self.url, current) == key:
                self.history.record(key, 'confirmed')
                self.confirmed += 1
                LOG.info('Submission confirmed')
                return
        except BrowserError:
            pass
        LOG.info('First-answer attempt complete; acceptance unconfirmed, will not retry')


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True, help='Poll Everywhere participant URL')
    parser.add_argument('--interval', type=float, default=2, help='Seconds between checks (default: 2)')
    parser.add_argument('--config', type=Path, help='JSON CSS selector overrides')
    parser.add_argument('--dry-run', action='store_true', help='Detect and log polls without voting')
    args = parser.parse_args(argv)
    parts = urlsplit(args.url)
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
        parser.error('--url must be an http(s) URL without embedded credentials')
    args.url = urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip('/'), parts.query, ''))
    if not math.isfinite(args.interval) or args.interval < 0.5:
        parser.error('--interval must be finite and at least 0.5 seconds')
    return args


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s', datefmt='%H:%M:%S')
    selectors = dict(DEFAULT_SELECTORS)
    watcher = None
    try:
        if sync_playwright is None:
            raise ValueError('Playwright is missing. Install it with: python -m pip install -r requirements.txt')
        if args.config:
            overrides = json.loads(args.config.read_text())
            if not isinstance(overrides, dict) or set(overrides) - set(selectors) or any(
                not isinstance(v, str) or not v.strip() for v in overrides.values()
            ):
                raise ValueError('Config must contain known selector names and nonempty CSS strings')
            selectors.update(overrides)
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
                watcher = Watcher(page, args.url, selectors, history, args.dry_run)
                LOG.info('Monitoring until Ctrl+C%s', ' (dry run)' if args.dry_run else '')
                failures = 0
                while not page.is_closed():
                    try:
                        watcher.tick()
                        failures = 0
                    except BrowserError as error:
                        if page.is_closed():
                            break
                        failures += 1
                        LOG.warning('Browser error; any recorded attempt will not be retried: %s', error.message.splitlines()[0])
                    try:
                        page.wait_for_timeout(min(30, args.interval * 2 ** min(failures, 4)) * 1000)
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
            LOG.info('Session: %d attempts, %d visibly confirmed', watcher.attempted, watcher.confirmed)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
