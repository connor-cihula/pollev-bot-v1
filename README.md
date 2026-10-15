## Description

Monitors a Poll Everywhere URL and votes the first answer anytime a new poll is posted. Designed for 1.0 version used in class, don't think it works on 2.0.

Can also keep sending responses to the first answer every second til you ^C it, default is 20 per second. Untested feature.

AI comments:

The built-in selectors recognize the Poll Everywhere 1.0 vote-button layout
captured from `https://pollev.com/kyragan244`. The bot uses the activity's response
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
python bot.py --url https://pollev.com/kyragan244
```
Or use whatever URL desired, no guarantees, might only work on bar polls.

Login through the browser that launches. Login state is saved in the browser the proj uses. Once monitoring the right page, hit enter in the terminal. In all runs except the first, this should be as smooth as enter and enter.

Terminate with ^C.

## Options

- `--interval <number>`: replace <number> with an int. Default is checking every 2 seconds.

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
