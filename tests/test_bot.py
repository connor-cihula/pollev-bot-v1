import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from bot import BrowserError, DEFAULT_SELECTORS, History, Watcher, main, parse_args, poll_key

URL = 'https://pollev.com/test'


def poll(identity='1', **changes):
    return dict(status='poll', id=identity, question='A question',
                options=[dict(index=0, text='First', selected=False, disabled=False),
                         dict(index=1, text='Second', selected=False, disabled=False)],
                answered=False, disabled=False, **changes)


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.history = History(Path(self.temp.name) / 'history.json')
        self.page = MagicMock(url=URL)
        self.page.evaluate.return_value = poll()
        self.option = self.page.locator.return_value.filter.return_value.locator.return_value.nth.return_value.element_handle.return_value
        self.option.evaluate.return_value = True
        self.option.is_visible.return_value = True
        self.submit = self.page.locator.return_value.filter.return_value.locator.return_value.filter.return_value
        self.submit.count.return_value = 0
        self.page.locator.return_value.filter.return_value.get_by_role.return_value.filter.return_value.count.return_value = 0
        self.page.wait_for_function.side_effect = BrowserError('Unconfirmed')
        self.watcher = Watcher(self.page, URL, DEFAULT_SELECTORS, self.history)

    def test_three_polls_and_repeated_updates(self):
        for identity in ('1', '2', '3'):
            self.page.evaluate.return_value = poll(identity)
            self.watcher.tick()
            self.watcher.tick()
        self.assertEqual(self.option.check.call_count, 3)
        self.assertEqual(self.watcher.attempted, 3)

    def test_restart_does_not_retry_uncertain_vote(self):
        self.watcher.tick()
        history = History(self.history.path)
        Watcher(self.page, URL, DEFAULT_SELECTORS, history).tick()
        self.assertEqual(self.option.check.call_count, 1)

    def test_write_precedes_click(self):
        def check(**kwargs):
            self.assertEqual(History(self.history.path).entries[poll_key(URL, poll())], 'attempted')
        self.option.check.side_effect = check
        self.watcher.tick()

    def test_hidden_radio_uses_label(self):
        self.option.is_visible.return_value = False
        self.watcher.tick()
        self.option.check.assert_not_called()
        self.option.evaluate_handle.return_value.as_element.return_value.click.assert_called_once()

    def test_failed_click_is_not_retried_after_recovery(self):
        self.option.check.side_effect = BrowserError('Network failure after click')
        with self.assertRaises(BrowserError):
            self.watcher.tick()
        self.option.check.side_effect = None
        self.watcher.tick()
        self.option.check.assert_called_once()

    def test_already_answered(self):
        value = poll()
        value['answered'] = True
        self.page.evaluate.return_value = value
        self.watcher.tick()
        self.option.check.assert_not_called()
        self.assertIn('already-answered', self.history.entries.values())

    def test_waiting_and_disabled(self):
        for value in ({'status': 'waiting', 'reason': 'Unsupported poll type'}, poll()):
            value['disabled'] = True
            self.page.evaluate.return_value = value
            self.watcher.tick()
        self.option.check.assert_not_called()

    def test_changed_poll_before_click(self):
        self.page.evaluate.side_effect = [poll('1'), poll('2')]
        self.watcher.tick()
        self.option.check.assert_not_called()
        self.assertFalse(self.history.entries)

    def test_changed_poll_does_not_submit(self):
        self.page.evaluate.side_effect = [poll('1'), poll('1'), poll('2'), poll('2')]
        self.watcher.tick()
        self.submit.click.assert_not_called()

    def test_explicit_submit_and_confirmation(self):
        self.submit.count.return_value = 1
        self.submit.is_enabled.return_value = True
        self.page.wait_for_function.side_effect = None
        self.watcher.tick()
        self.submit.element_handle.return_value.click.assert_called_once()
        self.assertEqual(self.watcher.confirmed, 1)

    def test_confirmation_timeout_stays_attempted(self):
        self.watcher.tick()
        self.assertEqual(set(self.history.entries.values()), {'attempted'})
        self.assertEqual(self.watcher.confirmed, 0)

    def test_login_redirect_never_clicks(self):
        self.page.url = 'https://school.edu/login'
        self.watcher.tick()
        self.page.evaluate.assert_not_called()

    def test_corrupted_history_fails_closed(self):
        self.history.path.write_text('{bad json')
        with self.assertRaises(ValueError):
            History(self.history.path)
        self.history.path.write_text(json.dumps({'key': 'unknown'}))
        with self.assertRaises(ValueError):
            History(self.history.path)

    def test_fallback_identity_and_distinct_ids(self):
        self.assertNotEqual(poll_key(URL, poll('1')), poll_key(URL, poll('2')))
        first = poll(None)
        second = copy.deepcopy(first)
        second['options'][0]['text'] = 'Different'
        self.assertNotEqual(poll_key(URL, first), poll_key(URL, second))
        self.assertNotEqual(poll_key(URL, first), poll_key(URL + 'other', first))

    def test_args_have_no_timer(self):
        args = parse_args(['--url', URL])
        self.assertEqual(args.interval, 2)
        self.assertFalse(hasattr(args, 'duration'))

    def test_ctrl_c_closes_browser_and_preserves_history(self):
        runtime = MagicMock()
        context = runtime.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value
        context.pages = [self.page]
        self.page.is_closed.return_value = False
        self.page.wait_for_timeout.side_effect = KeyboardInterrupt
        with patch('bot.sync_playwright', runtime), patch('bot.BASE', Path(self.temp.name)), patch('builtins.input', return_value=''):
            self.assertEqual(main(['--url', URL]), 0)
        context.close.assert_called_once()
        self.assertEqual(set(History(Path(self.temp.name) / '.local' / 'history.json').entries.values()), {'attempted'})

    def test_browser_closed_during_wait_is_clean_shutdown(self):
        runtime = MagicMock()
        context = runtime.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value
        context.pages = [self.page]
        self.page.is_closed.side_effect = [False, True]
        self.page.wait_for_timeout.side_effect = BrowserError('Target page has been closed')
        with patch('bot.sync_playwright', runtime), patch('bot.BASE', Path(self.temp.name)), patch('builtins.input', return_value=''):
            self.assertEqual(main(['--url', URL]), 0)
        context.close.assert_called_once()


@unittest.skipUnless(importlib.util.find_spec('playwright'), 'Playwright is not installed')
class BrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch()
        except BrowserError:
            cls.playwright.stop()
            raise  # Missing Chromium/system libraries must fail, not mask a broken install.

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.page = self.browser.new_page()
        self.addCleanup(self.page.close)
        # Serve fixtures through intercepted localhost requests; no external traffic.
        self.page.route('http://localhost/**', lambda route: route.fulfill(body='<main></main>', content_type='text/html'))
        self.page.goto('http://localhost/polls')
        self.watcher = Watcher(self.page, 'http://localhost/polls', DEFAULT_SELECTORS,
                               History(Path(self.temp.name) / 'history.json'))

    def render(self, identity='1', extra='', hidden=False):
        style = 'style="position:absolute;opacity:0;width:0;height:0"' if hidden else ''
        self.page.set_content(f'''<main data-poll-id="{identity}"><h2>Pick an answer</h2>
            <label><input type="radio" name="answer" value="a" {style}>First</label>
            <label><input type="radio" name="answer" value="b">Second</label>
            {extra}<div role="status"></div></main>
            <script>window.clicks = 0;
            document.querySelectorAll('input').forEach(e => e.onchange = () => {{
                window.clicks++;
                if (!document.querySelector('button'))
                    document.querySelector('[role=status]').innerText = 'Response received';
            }});</script>''')

    def test_live_poll_transitions_and_no_duplicates(self):
        for identity in ('1', '2', '3'):
            self.render(identity)
            self.watcher.tick()
            self.watcher.tick()
            self.assertEqual(self.page.evaluate('window.clicks'), 1)
            self.assertTrue(self.page.locator('input').first.is_checked())
            self.assertFalse(self.page.locator('input').nth(1).is_checked())
        self.assertEqual(self.watcher.confirmed, 3)

    def test_explicit_submission(self):
        self.render(extra='''<button type="submit" onclick="document.querySelector('[role=status]').innerText='Response submitted'">Submit</button>''')
        self.watcher.tick()
        self.assertEqual(self.watcher.confirmed, 1)

    def test_hidden_native_radio_label(self):
        self.render(hidden=True)
        self.watcher.tick()
        self.assertTrue(self.page.locator('input').first.is_checked())

    def test_unsupported_and_closed(self):
        for extra in ('<input type="checkbox">', '<span data-poll-closed="true">Closed</span>'):
            self.render(extra=extra)
            self.watcher.tick()
            self.assertEqual(self.page.evaluate('window.clicks'), 0)

    def test_already_answered(self):
        self.render()
        self.page.locator('input').nth(1).check()
        self.watcher.tick()
        self.assertEqual(self.watcher.attempted, 0)

    def render_legacy(self, identity='fixture-poll', voted=False, locked=False):
        # Sanitized structural excerpt of the user's before/after HTML. No
        # personal information, session tokens, third-party assets or scripts.
        fixture = (Path(__file__).parent / 'fixtures' / 'legacy_poll.html').read_text()
        self.page.set_content(fixture.replace('FIXTURE_POLL', identity))
        if voted:
            self.page.evaluate("""() => {
                document.querySelector('.component-response-multiple-choice__option').setAttribute('data-vote-count', '1');
                document.querySelector('.component-response-multiple-choice__option__vote').setAttribute('aria-label', 'First with your vote, option 1 of 4');
            }""")
        if locked:
            self.page.evaluate("""() => {
                document.querySelector('.component-response-header__status').innerText = 'This poll is locked.';
                document.querySelectorAll('.component-response-multiple-choice__option__vote').forEach(e => e.disabled = true);
            }""")

    def test_legacy_detects_buttons_heading_and_stable_identity(self):
        self.render_legacy()
        before = self.watcher.snapshot()
        self.assertEqual(before['question'], 'Fixture question')
        self.assertEqual(before['id'], 'pollev:/multiple_choice_polls/fixture-poll/respond')
        self.assertEqual(len(before['options']), 4)
        self.assertFalse(before['answered'])
        self.watcher.tick()
        after = self.watcher.snapshot()
        self.assertTrue(after['answered'])
        self.assertEqual(poll_key('http://localhost/polls', before), poll_key('http://localhost/polls', after))
        self.assertEqual(self.watcher.confirmed, 1)
        self.watcher.tick()
        self.assertEqual(self.page.evaluate('window.voteClicks'), [1, 0, 0, 0])
        self.assertEqual(self.page.evaluate('window.navigationClicks'), 0)

    def test_legacy_locked_after_vote_is_confirmation(self):
        self.render_legacy(voted=True, locked=True)
        state = self.watcher.snapshot()
        self.assertTrue(state['confirmed'])
        self.assertTrue(state['disabled'])
        self.watcher.tick()
        self.assertEqual(self.watcher.attempted, 0)
        self.assertEqual(self.page.evaluate('window.voteClicks'), [0, 0, 0, 0])

    def test_legacy_poll_changes_vote_once_each(self):
        for identity in ('first', 'second', 'third'):
            self.render_legacy(identity)
            self.watcher.tick()
            self.watcher.tick()
            self.assertEqual(self.page.evaluate('window.voteClicks'), [1, 0, 0, 0])
        self.assertEqual(self.watcher.confirmed, 3)

    def test_legacy_locked_unanswered_does_not_vote(self):
        self.render_legacy(locked=True)
        self.watcher.tick()
        self.assertEqual(self.watcher.attempted, 0)
        self.assertEqual(self.page.evaluate('window.voteClicks'), [0, 0, 0, 0])


if __name__ == '__main__':
    unittest.main()
