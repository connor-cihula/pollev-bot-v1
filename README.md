## Description

Monitors a Poll Everywhere URL and votes the first answer anytime a new poll is posted. Designed for 1.0 version used in class, don't think it works on 2.0.

Can also keep sending responses to the first answer every second til you ^C it, default is 20 per second. Untested feature.

AI comments:

The built-in selectors recognize the Poll Everywhere 1.0 vote-button layout
captured from a participant page. The bot uses the activity's response
URL as its identity, reads the question heading rather than "Live results", and
detects an existing vote from `data-vote-count`. Vote buttons submit immediately;
navigation tabs and undo buttons are excluded. Locked polls are skipped.


## Setup

Linux (probably works on others things idk  don't care):
```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m playwright install chromium
```

Here's what AI says about Windows and Mac tho:

On Windows PowerShell, use `python -m venv .venv`, then
`.venv\Scripts\Activate.ps1`, followed by the same pip and Playwright commands.
If PowerShell activation is unavailable, run `.venv\Scripts\python.exe` directly.
Likewise, macOS/Linux can use `.venv/bin/python` directly without activation.

The `.venv` isolates Python dependencies. Playwright separately downloads a
Chromium browser; its cache normally lives outside the project. No Docker is
needed. On Linux, if Chromium reports missing system libraries, install the
distribution's required libraries or run `python -m playwright install-deps
chromium` (may require administrator privileges).


## Run

```sh
.venv/bin/python bot.py --url https://pollev.com/YOUR_PRESENTER
```
Or use whatever URL desired, no guarantees, might only work on bar polls.

Login through the browser that launches. Login state is saved in the browser the proj uses. Once monitoring the right page, hit enter in the terminal. In all runs except the first, this should be as smooth as enter and enter.

Terminate with ^C.

## Options

- `--interval <number>`: replace <number> with an int. Default is checking every 2 seconds.

- `--count N`: attempt the first answer exactly N times per poll when it stays enabled. For five attempts:

```sh
.venv/bin/python bot.py --url https://pollev.com/YOUR_PRESENTER --count 5
```

Attempts run back to back with no added delay, as fast as the page accepts clicks.
For the legacy Poll Everywhere page, the batch calls the app's existing result
submission handler inside one browser call. Requests start together instead of
waiting for the page to re-enable its buttons after every response. Poll locks,
total response limits, and per-option limits are still checked. If the app's
model is unavailable, it falls back to clicks. Logs identify the path (`app` or
`click`) and report dispatch time in milliseconds; server processing may finish later.
`--interval` only controls idle monitoring and waiting after errors in this mode.
After reaching the count, the bot waits for a new poll and applies the same count there. Other answers are
untouched. Locked polls and disabled answers pause attempts. Each run starts a
fresh batch: restarting with `--count 5` attempts another five on the same poll.
Within a run, reloading or revisiting a poll does not reset its count. Earlier
saved count history does not block a new batch. This mode can run on an
already-answered poll. A failed or uncertain click consumes one
attempt. Logs count attempts, not confirmed accepted responses; a poll must allow
multiple responses to record multiple votes. `--count` and `--repeat` cannot be
combined.

- `--count_batches SIZE BATCHES`: send `SIZE` attempts at once, repeated `BATCHES`
  times, to the first answer of each poll. For three batches of five (15 total):

```sh
.venv/bin/python bot.py --url https://pollev.com/YOUR_PRESENTER --count_batches 5 3
```

Both numbers must be positive integers. Batches run consecutively with no added
delay and do not wait for previous requests to finish. The legacy app handler
still checks poll locks and response limits. Logs show each batch's progress.
Each run starts fresh, just like `--count`. Use only one of `--count_batches`,
`--count`, or `--repeat` at a time.

- `--repeat [number]`: keep voting the first answer til you ^C it. Default is 20 attempts per second, or use `--repeat 5` for 5 per second. Keeps going on the same poll and follows new ones too. Untested.

```sh
python bot.py --url https://pe.app/elemehnop --repeat
python bot.py --url https://pe.app/elemehnop --repeat 5
```

Poll needs to allow multiple responses. Pauses if it's locked or the answer is disabled. Actual responses per second depend on how fast the page accepts clicks, no guarantees it'll hit 20. `--interval` only controls waiting after errors in repeat mode.


## Testing

```sh
python -m unittest discover -s tests -v
```

AI says:
Core state-machine tests use only the Python standard library. Browser tests
use a simulated local participant page and require Playwright plus Chromium;
they are reported as skipped when Playwright is absent. They do not access or
vote on Poll Everywhere. A sanitized fixture reproduces the supplied before/after
vote-button structure, including the locked state after voting. A live
compatibility check still needs to be performed on the laptop.

To run just the core tests without launching Chromium:

```sh
python -m unittest discover -s tests -k StateTests -v
```
