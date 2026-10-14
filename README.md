# agy-gemini

Claude Code combines two models. Claude (for example Opus) makes the decisions: the plan, short specs, the review, and the final check. Gemini 3.8 Flash, through the agy CLI, does the long work: reading large text and project files, and writing code, tests, and documents. Gemini has no tools; Claude applies changes and runs all commands. This saves Claude tokens. The agy CLI is the Google Antigravity CLI.

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
With --read-tools: Gemini asks to READ, SEARCH, or LIST project files; the script answers automatically (several rounds)
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

## How Claude and Gemini share the work

| Phase | Who | Notes |
|---|---|---|
| 1. Clarify the request | Claude, with the user | |
| 2. Collect context (code, logs, issues, docs) | Gemini | Use `--read-tools`, or send files with `--file`. Ask for exact quotes. |
| 3. Decide the approach and the plan | Claude | Keep it short. |
| 4. Write a spec for each step | Claude | Use the spec template in SKILL.md. |
| 5. Write the code | Gemini | One part per call. See "Size of one call" in SKILL.md. |
| 6. Write the tests | Gemini, in a **separate** conversation | Tests follow the spec, not the code. |
| 7. Run tests, lint, and build | Claude runs them | |
| 8. Fix failures | Gemini, given the exact error | After 2 failed rounds, Claude takes over. |
| 9. Review | Claude | Depth depends on the risk level. |
| 10. Docs, commit messages, translation | Gemini | Claude checks the facts. |
| 11. Report to the user | Claude | |

- **Low** (tests, docs, scripts, simple glue code): Gemini writes. Claude checks with tests and a quick skim for dangerous operations.
- **Medium** (normal business logic): Gemini writes. Claude reads the full diff.
- **High** (money and ledgers, authentication, permissions, secrets, file access, migrations, concurrency): Claude writes the spec in detail and reads every line of the diff. If the change is small or subtle, Claude writes it.

The full rules for Claude are in agy-gemini/SKILL.md.

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

Find code:
```
use gemini to find where the retry logic is implemented and quote the lines
```
Claude then uses `--read-tools`, so Gemini finds the files itself.

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

With --read-tools:
- Only files inside the project folder.
- Protected files (.env, keys, .git and similar) are refused.
- A file with secret-looking content is not shown at all.
- SEARCH hides lines with secret-looking content.
- Folders such as node_modules, target, build, and .venv are skipped.
- When the project folder is the top level of a git repository, git-ignored files are not listed or searched.

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
- Gemini's reply has an output limit of about 65,000 tokens, and its thinking counts toward it, so large tasks must be split into parts (this happened during development).
- With --read-tools: one READ shows at most 60 KB, SEARCH returns at most 50 matching lines, LIST returns at most 200 paths, and the default is at most 8 request rounds (--max-reads). Paths with spaces are not supported in read requests.

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
- `--read-tools`: Gemini may ask to read, search, or list project files.
- `--max-reads N`: maximum number of request rounds (default 8).

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
