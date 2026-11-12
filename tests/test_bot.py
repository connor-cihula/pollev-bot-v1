import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from bot import BrowserError, History, SNAPSHOT, Watcher, main, next_repeat_slot, parse_args, poll_key

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
        self.watcher = Watcher(self.page, URL, self.history)

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
        Watcher(self.page, URL, history).tick()
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

    def test_repeat_defaults_and_custom_rate(self):
        self.assertIsNone(parse_args(['--url', URL]).repeat)
        self.assertEqual(parse_args(['--url', URL, '--repeat']).repeat, 20)
        self.assertEqual(parse_args(['--url', URL, '--repeat', '5']).repeat, 5)
        for value in ('0', '-1', '1.5'):
            with patch('sys.stderr'), self.assertRaises(SystemExit):
                parse_args(['--url', URL, '--repeat', value])

    def test_repeat_pacing_skips_missed_slots(self):
        self.assertAlmostEqual(next_repeat_slot(0, .01, .05), .05)
        self.assertAlmostEqual(next_repeat_slot(.05, .08, .05), .10)
        self.assertAlmostEqual(next_repeat_slot(.10, .31, .05), .35)
        self.assertAlmostEqual(next_repeat_slot(0, 3, .05), 3)

    def test_repeat_votes_even_if_answered_and_in_history(self):
        self.watcher.repeat = True
        value = poll()
        value['answered'] = True
        self.page.evaluate.return_value = value
        self.history.record(poll_key(URL, value), 'confirmed')
        for _ in range(20):
            self.watcher.tick()
        self.assertEqual(self.option.click.call_count, 20)
        self.assertEqual(self.watcher.attempted, 20)
        self.option.check.assert_not_called()
        self.page.wait_for_function.assert_not_called()

    def test_repeat_button_clicks_first_answer_and_follows_new_polls(self):
        self.watcher.repeat = True
        self.option.evaluate.return_value = False
        for identity in ('1', '2'):
            self.page.evaluate.return_value = poll(identity)
            for _ in range(3):
                self.watcher.tick()
        self.assertEqual(self.option.click.call_count, 6)
        self.assertEqual(len(self.history.entries), 2)
        self.assertEqual(self.watcher.confirmed, 0)

    def test_repeat_pauses_when_disabled_and_resumes(self):
        self.watcher.repeat = True
        value = poll()
        value['disabled'] = True
        self.page.evaluate.return_value = value
        self.watcher.tick()
        self.option.click.assert_not_called()
        value['disabled'] = False
        self.watcher.tick()
        self.option.click.assert_called_once()

    def test_repeat_still_rechecks_identity_before_click(self):
        self.watcher.repeat = True
        self.page.evaluate.side_effect = [poll('1'), poll('2')]
        self.watcher.tick()
        self.option.click.assert_not_called()

    def test_repeat_ctrl_c_closes_browser(self):
        runtime = MagicMock()
        context = runtime.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value
        context.pages = [self.page]
        self.page.is_closed.return_value = False
        self.page.wait_for_timeout.side_effect = KeyboardInterrupt
        with patch('bot.sync_playwright', runtime), patch('bot.BASE', Path(self.temp.name)), patch('builtins.input', return_value=''):
            self.assertEqual(main(['--url', URL, '--repeat']), 0)
        self.option.click.assert_called_once()
        context.close.assert_called_once()

    def test_count_args(self):
        self.assertIsNone(parse_args(['--url', URL]).count)
        self.assertEqual(parse_args(['--url', URL, '--count', '5']).count, 5)
        for extra in (['--count'], ['--count', '0'], ['--count', '-1'],
                      ['--count', '1.5'], ['--count', '5', '--repeat']):
            with patch('sys.stderr'), self.assertRaises(SystemExit):
                parse_args(['--url', URL, *extra])

    def test_count_batches_args(self):
        self.assertIsNone(parse_args(['--url', URL]).count_batches)
        self.assertEqual(parse_args(['--url', URL, '--count_batches', '5', '3']).count_batches, [5, 3])
        for extra in ([], ['5'], ['5', '3', '2'], ['0', '3'], ['5', '-1'],
                      ['1.5', '3'], ['5', '3', '--count', '5'], ['5', '3', '--repeat']):
            with self.subTest(extra=extra), patch('sys.stderr'), self.assertRaises(SystemExit):
                parse_args(['--url', URL, '--count_batches', *extra])

    def test_count_batches_sizes_cap_and_new_polls(self):
        value = poll(legacy=True)
        limits = []
        def evaluate(script, arg):
            if script == SNAPSHOT:
                return value
            limits.append(arg['limit'])
            value['disabled'] = True  # Pending requests must not stall batches.
            return dict(attempted=arg['limit'], elapsed_ms=0, transport='app')
        self.page.evaluate.side_effect = evaluate
        watcher = Watcher(self.page, URL, self.history, count_batches=(2, 3))
        for identity in ('1', '2', '1'):
            value['id'] = identity
            for _ in range(4):
                watcher.tick()
        self.assertEqual(limits, [2] * 6)
        self.assertEqual(watcher.attempted, 12)

    def test_count_batches_main_runs_consecutively(self):
        runtime = MagicMock()
        context = runtime.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value
        context.pages = [self.page]
        self.page.is_closed.side_effect = [False] * 4 + [True]
        def evaluate(script, arg):
            if script == SNAPSHOT:
                return poll(legacy=True)
            self.page.wait_for_timeout.assert_not_called()
            self.assertEqual(arg['limit'], 2)
            return dict(attempted=2, elapsed_ms=0, transport='app')
        self.page.evaluate.side_effect = evaluate
        with patch('bot.sync_playwright', runtime), patch('bot.BASE', Path(self.temp.name)), patch('builtins.input', return_value=''):
            self.assertEqual(main(['--url', URL, '--count_batches', '2', '3']), 0)
        self.page.wait_for_timeout.assert_called_once_with(2000)
        context.close.assert_called_once()

    def test_count_caps_attempts_even_if_already_answered(self):
        self.watcher.count = 5
        value = poll()
        value['answered'] = True
        self.page.evaluate.return_value = value
        self.history.record(poll_key(URL, value), 'confirmed')
        for _ in range(8):
            self.watcher.tick()
        self.assertEqual(self.option.click.call_count, 5)
        self.assertEqual(self.watcher.attempted, 5)
        self.option.check.assert_not_called()
        self.page.wait_for_function.assert_not_called()

    def test_count_starts_fresh_after_restart(self):
        self.watcher.count = 5
        for _ in range(5):
            self.watcher.tick()
        restarted = Watcher(self.page, URL, History(self.history.path), count=5)
        for _ in range(5):
            restarted.tick()
        self.assertEqual(self.option.click.call_count, 10)
        self.assertEqual(restarted.attempted, 5)
        # Returning to default mode must not add another attempt either.
        Watcher(self.page, URL, History(self.history.path)).tick()
        self.option.check.assert_not_called()

    def test_count_ignores_old_saved_budget(self):
        key = poll_key(URL, poll())
        self.history.record(key, 'attempted')
        for index in range(1, 6):
            self.history.record(f'{key}:count:{index}', 'attempted')
        self.watcher.count = 5
        for _ in range(8):
            self.watcher.tick()
        self.assertEqual(self.option.click.call_count, 5)

    def test_count_reserves_budget_before_uncertain_click(self):
        self.watcher.count = 5
        def fail(**kwargs):
            saved = History(self.history.path)
            key = poll_key(URL, poll())
            self.assertEqual(saved.entries[key], 'attempted')
            self.assertEqual(self.watcher.count_attempts[key], 1)
            raise BrowserError('Uncertain click')
        self.option.click.side_effect = fail
        with self.assertRaises(BrowserError):
            self.watcher.tick()
        self.option.click.side_effect = None
        for _ in range(6):
            self.watcher.tick()
        self.assertEqual(self.option.click.call_count, 5)

    def test_count_budget_is_per_poll_and_can_be_increased(self):
        self.watcher.count = 5
        for identity in ('1', '2', '1'):
            self.page.evaluate.return_value = poll(identity)
            for _ in range(6):
                self.watcher.tick()
        self.assertEqual(self.option.click.call_count, 10)
        self.watcher.count = 6
        self.watcher.tick()
        self.watcher.tick()
        self.assertEqual(self.option.click.call_count, 11)

    def test_uncertain_burst_is_not_repeated(self):
        self.watcher.count = 5
        value = poll(legacy=True)
        self.page.evaluate.side_effect = [value, BrowserError('Browser context lost after dispatch')]
        with self.assertRaises(BrowserError):
            self.watcher.tick()
        self.page.evaluate.side_effect = None
        self.page.evaluate.return_value = value
        self.watcher.tick()
        self.assertEqual(self.watcher.attempted, 5)
        self.assertEqual(self.watcher.count_attempts[poll_key(URL, value)], 5)
        self.assertEqual(self.page.evaluate.call_count, 3)

    def test_count_does_not_consume_budget_while_disabled_or_changing(self):
        self.watcher.count = 5
        value = poll()
        value['disabled'] = True
        self.page.evaluate.return_value = value
        self.watcher.tick()
        self.page.evaluate.side_effect = [poll('1'), poll('2')]
        self.watcher.tick()
        self.assertFalse(self.history.entries)
        self.option.click.assert_not_called()

    def test_count_main_sends_without_waiting_then_uses_idle_interval(self):
        runtime = MagicMock()
        context = runtime.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value
        context.pages = [self.page]
        self.page.is_closed.side_effect = [False] * 7 + [True]
        self.option.click.side_effect = lambda **kwargs: self.page.wait_for_timeout.assert_not_called()
        with patch('bot.sync_playwright', runtime), patch('bot.BASE', Path(self.temp.name)), patch('builtins.input', return_value=''):
            self.assertEqual(main(['--url', URL, '--count', '5', '--interval', '0.5']), 0)
        self.assertEqual(self.option.click.call_count, 5)
        self.assertEqual(self.page.wait_for_timeout.call_count, 2)
        self.assertTrue(all(call.args == (500,) for call in self.page.wait_for_timeout.call_args_list))
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
        self.watcher = Watcher(self.page, 'http://localhost/polls',
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

    def test_legacy_repeat_twenty_responses_to_first_answer(self):
        self.render_legacy()
        # Simulate a poll allowing multiple responses: it updates the own-vote
        # count without disabling its response buttons after the first vote.
        self.page.evaluate("""() => {
            document.querySelectorAll('.component-response-multiple-choice__option__vote').forEach((button, index) => {
                button.onclick = () => {
                    window.voteClicks[index]++;
                    button.parentElement.setAttribute('data-vote-count', String(window.voteClicks[index]));
                };
            });
        }""")
        self.watcher.repeat = True
        for _ in range(20):
            self.watcher.tick()
        self.assertEqual(self.page.evaluate('window.voteClicks'), [20, 0, 0, 0])
        self.assertEqual(self.watcher.attempted, 20)
        self.assertEqual(self.page.evaluate('window.navigationClicks'), 0)

    def test_legacy_count_five_responses_to_first_answer(self):
        self.render_legacy()
        self.page.evaluate("""() => {
            document.querySelectorAll('.component-response-multiple-choice__option__vote').forEach((button, index) => {
                button.onclick = () => {
                    window.voteClicks[index]++;
                    button.parentElement.setAttribute('data-vote-count', String(window.voteClicks[index]));
                };
            });
        }""")
        self.watcher.count = 5
        started = time.perf_counter()
        self.watcher.tick()
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 0.5, f'Five-answer burst took {elapsed:.3f}s')
        for _ in range(3):
            self.watcher.tick()
        self.assertEqual(self.page.evaluate('window.voteClicks'), [5, 0, 0, 0])
        self.assertEqual(self.watcher.attempted, 5)
        self.assertEqual(self.page.evaluate('window.navigationClicks'), 0)

    def test_legacy_count_reacquires_button_after_render(self):
        self.render_legacy()
        self.page.evaluate("""() => {
            const wire = button => {
                button.onclick = () => {
                    window.voteClicks[0]++;
                    const replacement = button.cloneNode(true);
                    button.replaceWith(replacement);
                    wire(replacement);
                };
            };
            wire(document.querySelector('.component-response-multiple-choice__option__vote'));
        }""")
        self.watcher.count = 5
        self.watcher.tick()
        self.assertEqual(self.page.evaluate('window.voteClicks'), [5, 0, 0, 0])

    def test_legacy_count_stops_burst_when_poll_changes(self):
        self.render_legacy()
        self.page.evaluate("""() => {
            const button = document.querySelector('.component-response-multiple-choice__option__vote');
            button.onclick = () => {
                window.voteClicks[0]++;
                document.querySelector('turbo-frame[src]').setAttribute('src', '/multiple_choice_polls/new/respond');
            };
        }""")
        self.watcher.count = 5
        self.watcher.tick()
        self.assertEqual(self.page.evaluate('window.voteClicks'), [1, 0, 0, 0])
        self.assertEqual(self.watcher.attempted, 1)

    def test_legacy_count_stops_if_poll_locks_after_first_vote(self):
        self.render_legacy()
        self.watcher.count = 5
        for _ in range(6):
            self.watcher.tick()
        self.assertEqual(self.page.evaluate('window.voteClicks'), [1, 0, 0, 0])
        self.assertEqual(self.watcher.attempted, 1)

    def render_pending_legacy(self, maximum=0, per_option=0):
        self.render_legacy()
        self.page.evaluate("""({maximum, perOption}) => {
            // Reproduce the observed legacy app contract: each 'result' event
            // saves one result and disables all buttons until requests finish.
            // Keep each response pending for a second to expose serialization.
            window.dispatches = [];
            window.completed = 0;
            const records = [];
            const options = [1, 2, 3, 4].map(id => ({id}));
            const buttons = [...document.querySelectorAll('.component-response-multiple-choice__option__vote')];
            const update = () => {
                buttons.forEach(b => b.disabled = records.some(r => r.has('isPending')));
            };
            const results = {
                get length() {return records.length;},
                filter: fn => records.filter(fn),
                add: attrs => {
                    const result = {
                        attrs, get: key => attrs[key],
                        has: key => Object.hasOwn(attrs, key)
                    };
                    records.push(result);
                    return result;
                }
            };
            const view = {trigger: (event, result, option) => {
                if (event !== 'result') throw new Error('Unexpected event');
                result.attrs.isPending = true;
                update();
                window.dispatches.push({time: performance.now(), option: option.id});
                fetch('/responses', {method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({option_id: result.get('option_id')})
                }).then(() => setTimeout(() => {
                    delete result.attrs.isPending;
                    window.completed++;
                    update();
                }, 1000));
            }};
            const model = {
                type: () => 'multiple_choice_poll',
                get: key => ({max_votes: maximum, type_options: {responses_per_option: perOption}})[key],
                isStarted: () => true, isWebEnabled: () => true, isMaxedOut: () => false,
                results, mcpOptions: {at: index => options[index]}
            };
            document.querySelector('legacy-response-view').responseView = {model, view};
            buttons.forEach((button, index) => button.onclick = () => {
                const result = results.add({option_id: options[index].id});
                view.trigger('result', result, options[index]);
            });
        }""", {'maximum': maximum, 'perOption': per_option})

    def test_count_dispatches_five_requests_while_first_is_pending(self):
        self.render_pending_legacy()
        requests = []
        self.page.on('request', lambda request: requests.append(request.post_data_json)
                     if request.url.endswith('/responses') else None)
        self.watcher.count = 5
        started = time.perf_counter()
        self.watcher.tick()
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 0.5, f'Five concurrent submissions took {elapsed:.3f}s')
        state = self.page.evaluate('({dispatches: window.dispatches, completed: window.completed})')
        self.assertEqual(len(state['dispatches']), 5)
        self.assertEqual(state['completed'], 0)
        self.assertLess(state['dispatches'][-1]['time'] - state['dispatches'][0]['time'], 500)
        self.page.wait_for_function('window.completed === 5')
        self.assertEqual(requests, [{'option_id': 1}] * 5)
        self.assertEqual(self.watcher.attempted, 5)
        self.watcher.tick()
        self.assertEqual(len(self.page.evaluate('window.dispatches')), 5)

    def test_count_batches_dispatch_concurrently_and_stop_at_total(self):
        self.render_pending_legacy()
        watcher = Watcher(self.page, 'http://localhost/polls', self.watcher.history,
                          count_batches=(5, 3))
        requests = []
        self.page.on('request', lambda request: requests.append(request.post_data_json)
                     if request.url.endswith('/responses') else None)
        started = time.perf_counter()
        for total in (5, 10, 15):
            watcher.tick()
            self.assertEqual(watcher.attempted, total)
            self.assertEqual(len(self.page.evaluate('window.dispatches')), total)
        self.assertLess(time.perf_counter() - started, 0.5)
        self.assertEqual(self.page.evaluate('window.completed'), 0)
        watcher.tick()
        self.assertEqual(watcher.attempted, 15)
        self.page.wait_for_function('window.completed === 15')
        self.assertEqual(requests, [{'option_id': 1}] * 15)

    def test_count_batches_respect_response_limit_between_batches(self):
        self.render_pending_legacy(maximum=7)
        watcher = Watcher(self.page, 'http://localhost/polls', self.watcher.history,
                          count_batches=(5, 3))
        for _ in range(4):
            watcher.tick()
        self.assertEqual(watcher.attempted, 7)
        self.assertEqual(len(self.page.evaluate('window.dispatches')), 7)

    def test_count_app_path_respects_total_and_per_option_limits(self):
        for maximum, per_option, expected in ((1, 0, 1), (0, 2, 2), (3, 4, 3)):
            with self.subTest(maximum=maximum, per_option=per_option):
                self.render_pending_legacy(maximum, per_option)
                watcher = Watcher(self.page, 'http://localhost/polls', self.watcher.history, count=5)
                watcher.tick()
                self.assertEqual(watcher.attempted, expected)
                self.assertEqual(len(self.page.evaluate('window.dispatches')), expected)

    def test_count_app_path_stops_on_locked_model(self):
        self.render_pending_legacy()
        self.page.evaluate("document.querySelector('legacy-response-view').responseView.model.isStarted = () => false")
        self.watcher.count = 5
        self.watcher.tick()
        self.assertEqual(self.watcher.attempted, 0)
        self.assertEqual(self.page.evaluate('window.dispatches'), [])

    def test_count_app_path_stops_if_dom_locks_while_pending(self):
        self.render_pending_legacy()
        self.page.evaluate("""() => {
            const view = document.querySelector('legacy-response-view').responseView.view;
            const submit = view.trigger;
            view.trigger = (...args) => {
                submit(...args);
                document.querySelector('.component-response-header__status').innerText = 'This poll is locked.';
            };
        }""")
        self.watcher.count = 5
        self.watcher.tick()
        self.assertEqual(self.watcher.attempted, 1)
        self.assertEqual(len(self.page.evaluate('window.dispatches')), 1)


if __name__ == '__main__':
    unittest.main()
