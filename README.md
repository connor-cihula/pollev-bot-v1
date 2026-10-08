## Description

Monitors a Poll Everywhere URL and votes the first answer anytime a new poll is posted


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
python bot.py --url https://pollev.com/YOUR_PRESENTER
```

Login through the browser that launches. Login state is saved in the browser the proj uses.
Terminate with ^C.

The default selectors now recognize the Poll Everywhere 1.0 vote-button layout
captured from `https://pollev.com/kyragan244`. The bot uses the activity's response
URL as its identity, reads the question heading rather than "Live results", and
detects an existing vote from `data-vote-count`. Vote buttons submit immediately;
navigation tabs and undo buttons are excluded. Locked polls are skipped.


## Options

- `--interval <number>`: replace <number> with an int. Default to checking updates every 2 seconds.ng sign-in or navigation to another hostname it pauses voting.

- `python bot.py --url https://pollev.com/YOUR_PRESENTER --dry-run` inspect detection without casting a vote.


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
