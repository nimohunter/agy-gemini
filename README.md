# agy-gemini

Claude Code uses Gemini 3.8 Flash as a plain text model through the agy CLI.
The agy CLI is the Google Antigravity CLI.
Gemini works like an API.
Gemini reads long input and writes long output.
Claude is the agent and supervisor.
Claude picks files, reviews every result, applies changes, and runs commands.
This saves Claude tokens.

## How it works

```
User asks Claude ("use gemini ...")
  │
  ▼
Claude runs agy_run.py ask
  │
  ▼
agy runs agent "agy-gemini-plain" in an empty folder (guard hook active)
  │
  ▼
Gemini answers
  │
  ▼
For edits, script builds proposed diff and writes nothing
  │
  ▼
Claude reviews the diff
  │
  ▼
Claude runs agy_run.py apply
  │
  ▼
Claude runs the project's tests itself
```

## Requirements

- Claude Code.
- The agy CLI installed and logged in (tested with agy 1.3.4 and 1.3.5).
- Python 3.8 or newer.
- Linux or macOS with `sh`.

The skill was tested only on Linux (WSL2).
Windows is not supported, because the hook uses `sh`.

## Install

Clone the repository:

```bash
git clone https://github.com/nimohunter/agy-gemini.git
```

Copy the skill folder into your Claude skills directory:

```bash
cp -r agy-gemini/agy-gemini ~/.claude/skills/
```

To get updates with `git pull`, create a symbolic link instead:

```bash
ln -s "$PWD/agy-gemini/agy-gemini" ~/.claude/skills/agy-gemini
```

Run setup and doctor:

```bash
~/.claude/skills/agy-gemini/agy_run.py setup
~/.claude/skills/agy-gemini/agy_run.py doctor
```

The `setup` command writes `~/.gemini/config/agents/agy-gemini-plain.md`.
It adds one entry named "agy-gemini-guard" to `~/.gemini/config/hooks.json`.
It keeps other hooks and saves a backup of the old hooks file.
If `hooks.json` is not valid JSON, setup stops and changes nothing.

The `doctor` command checks the setup and the model names.
Then it makes 3 small live calls:
1. It checks that the plain agent has no tools.
2. It checks that the guard hook blocks tool calls.
3. It checks that a 114 KB message arrives complete.

## Usage

In Claude Code, say:
- `use gemini <task>`
- `ask gemini <task>`
- `agy <task>`
- `/agy-gemini <task>`

Examples:

Question about files:
```
use gemini: explain how authentication works in src/auth.py and quote the lines
```

Code change:
```
ask gemini to add input validation to src/login.py and write tests in tests/test_login.py
```

Follow-up:
```
gemini continue: handle the case where the user token is expired
```

## When to use it

Good jobs:
- Read large text and extract facts: issues, logs, specs, documentation.
- Translate or summarize text.
- Answer questions about code that you send.
- Small, clear code changes in a few files.
- Write tests for code that you send.

Bad jobs (do them yourself, or tell the user):
- Anything that needs to explore the repository or run code. Gemini has no tools.
- Debugging that needs many steps of trying and checking.
- Large refactors across many files.
- Anything that needs secrets.

## Safety

Safety layers for every Gemini call:
1. The agy agent "agy-gemini-plain" uses excludeDefaultComponents, so it has no built-in tools.
2. agy runs in an empty folder, not in the project.
3. A PreToolUse hook denies every tool call when AGY_GEMINI_GUARD is set.
4. Secret files and secret-looking content are blocked before anything is sent.
5. Edits are only proposals. Paths are checked. Nothing is written until `apply`.

What it does not protect:
- Everything you send goes to Google.
- The secret scan uses patterns. It can miss secrets or report false alarms.
- Text from issues or web pages can contain instructions. Gemini has no tools, so such text cannot make Gemini act. But Claude must still check that the answer follows the task.

About the hook: the entry in `~/.gemini/config/hooks.json` is global.
It does nothing unless `AGY_GEMINI_GUARD` is set, and only `agy_run.py` sets it.
So your own agy sessions are not affected.

## Limits

- One request is at most 150 KB including files.
- agy cuts longer messages (tested: 159 KB complete, 287 KB cut).
- The skill depends on agy behavior that is not documented.
- agy updates itself, so run doctor after agy updates.
- The Claude Code allowed-tools permission lasts only for the turn that starts the skill.
- Later follow-ups may ask for permission.
- Prompts, answers, and old file versions for undo are kept in `~/.cache/agy-gemini/runs/` (the newest 30 tasks).

## Configuration

Configuration (environment variables):
```
  AGY_GEMINI_MODEL_HIGH / _MEDIUM / _LOW   agy model names (see `agy models`)
  AGY_GEMINI_KEEP_TASKS                    number of saved tasks to keep (default 30)
```

## Direct use of the script

Commands:
```
  setup                 Install the agy agent and the guard hook. Run once, and after updates.
  doctor [--offline]    Check the setup and test agy's behavior. Run after every agy update.
  ask [options] [TASK]  Send a task to Gemini. TASK is read from stdin when it is not given.
  apply TASK_ID         Write the latest proposed changes of a task to disk.
  revert TASK_ID        Undo all applied changes of a task.
  uninstall [--purge]   Remove the agent and the hook. --purge also deletes the saved tasks.
```

Options for `ask`:
- `--model`: high, medium, low, or a full agy model name.
- `--file`: send this file to Gemini (repeatable).
- `--edit`: ask for edit blocks and build a proposed diff.
- `--continue`: follow-up in the same Gemini conversation.
- `--timeout`: seconds for Gemini (default 480).
- `--allow-secrets`: skip the secret scan (only with the user's consent).

## Uninstall

Run the uninstall command:

```bash
~/.claude/skills/agy-gemini/agy_run.py uninstall
```

Add `--purge` to delete saved tasks:

```bash
~/.claude/skills/agy-gemini/agy_run.py uninstall --purge
```

Then remove the skill folder:

```bash
rm -rf ~/.claude/skills/agy-gemini
```

## Development

Run the test suite:

```bash
python3 -m unittest discover -s tests
```

The tests never call agy or Gemini.

## Terms of service

This project automates the agy CLI. Check the Google Antigravity terms of service yourself before you use it.
This project is not affiliated with Google or Anthropic.

## License

MIT, see LICENSE.
