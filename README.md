# cheap_subagents

[![test](https://github.com/The-Guard305/cheap-subagents/actions/workflows/test.yml/badge.svg)](https://github.com/The-Guard305/cheap-subagents/actions/workflows/test.yml)

**One file. No dependencies. Stops Claude Code from running every subagent on your most expensive model.**

If you've hit the "your subagent use is heavy, try cheaper models" message — or you just watch your Opus budget evaporate on `Explore` agents that grep for a filename — this is the fix. It's a single Python `PreToolUse` hook that intercepts every `Agent` call and sets `model` to `haiku` or `sonnet` unless the task actually needs the big model.

```
Explore / claude-code-guide / "find all usages of X"   →  haiku
general-purpose / custom agents / "implement Y"        →  sonnet
Plan / "security review" / "root cause" / "trade-offs" →  untouched (inherits parent, e.g. opus)
[model:opus] anywhere in the prompt                    →  forced, tag stripped
```

## Why not just set `model:` in agent frontmatter?

Custom agents in `.claude/agents/*.md` can pin a model in their frontmatter, and if every subagent you spawn were one of those, you wouldn't need this. Two things get in the way:

- **The built-in types have no frontmatter you can edit.** `Explore`, `general-purpose`, and `Plan` ship inside Claude Code. There's no file to add `model: haiku` to, and those are exactly the types that burn the most calls.
- **The parent picks the type ad hoc.** The main session decides at call time whether a task is an `Explore`, a `general-purpose`, or an inline fork, and it rarely passes `model` unless prompted. Frontmatter only helps when the parent happens to choose an agent you've pre-configured.

A `PreToolUse` hook sits underneath both of those: it sees every `Agent` call regardless of type and can set `model` on the way through. Frontmatter is still the right tool for a custom agent that should *always* run on a specific model; this hook handles everything else.

## Install

```bash
curl -fsSLO https://raw.githubusercontent.com/The-Guard305/cheap-subagents/v0.1.0/cheap_subagents.py
mkdir -p ~/.claude && mv cheap_subagents.py ~/.claude/
python3 ~/.claude/cheap_subagents.py --install        # all projects (~/.claude/settings.json)
# or:  --install --project                              # just this repo (.claude/settings.json)
```

Restart Claude Code. `/hooks` should list an `Agent` matcher pointing at the script. That's it.

Uninstall: `python3 ~/.claude/cheap_subagents.py --uninstall`

## Proof it works

<!-- TODO: replace the table below with the output of `python3 ~/.claude/cheap_subagents.py --stats` once there are a few days of real routing data. -->

Run from a scratch project with the hook installed, Claude Code 2.1.305, `claude -p` asked to spawn one `Explore` subagent with no `model` parameter. Numbers are from Claude Code's own `modelUsage` accounting in `--output-format stream-json`:

| | subagent ran on | subagent cost | session total |
|---|---|---|---|
| **with hook** | `claude-haiku-5-5` | $0.0033 | $0.0663 |
| **without hook** | `claude-sonnet-5-5` (inherited from parent) | — | $0.0729 |

Routing log entry from that run:
```json
{"subagent_type": "Explore", "from": null, "to": "haiku", "reason": "type:Explore", "desc": "List markdown files"}
```

The toy task is tiny, so the parent's cache write dominates the total. On a real session where the parent is Opus and it fans out a dozen `Explore` / `general-purpose` agents, every one of those inherits Opus without this hook. That's the bill this fixes.

Run `python3 ~/.claude/cheap_subagents.py --stats` after a few days to see your own split.

## How it works

Claude Code fires a `PreToolUse` hook before every tool call. A hook that prints

```json
{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow", "updatedInput": {...}}}
```

replaces the tool's arguments. This script does exactly that for the `Agent` tool, adding a `model` field based on:

1. **Escape-hatch tag** — `[model:opus|sonnet|haiku|keep]` in the prompt wins and is stripped before the subagent sees it.
2. **Heavy signals** — regexes like `architect`, `trade-offs`, `root cause`, `review`, `critique`, `race condition` → leave `model` alone so the subagent inherits the parent.
3. **Agent type** — `Explore`, `claude-code-guide`, `statusline-setup` → haiku. `general-purpose` → sonnet (drops to haiku if the prompt is short and looks like find/list/summarize). `Plan` → inherit.
4. **Unknown/custom types** — scored by prompt: trivial → haiku, else sonnet.
5. **Never upgrade** — if the parent already asked for something cheaper, keep it.

Every decision is appended to `~/.claude/cheap_subagents.jsonl`.

## Configuration

Environment variables (put them in your shell or in `settings.json` under `"env"`):

| var | default | effect |
|---|---|---|
| `CHEAP_SUBAGENTS` | `1` | `0` disables routing |
| `CHEAP_SUBAGENTS_CHEAP` | `haiku` | model for trivial work |
| `CHEAP_SUBAGENTS_MID` | `sonnet` | model for normal work |
| `CHEAP_SUBAGENTS_RESPECT_EXPLICIT` | `0` | `1` = never override a `model` the parent set |
| `CHEAP_SUBAGENTS_LOG` | `~/.claude/cheap_subagents.jsonl` | audit log path; empty string disables |

To tune the heuristics, edit `TYPE_TIERS`, `HEAVY_PATTERNS`, `TRIVIAL_PATTERNS` at the top of the file. They're plain Python lists of regexes.

## Pairs well with

**Ralph-style fresh-process loops.** If you drive Claude Code from a shell loop that starts a new `claude -p` process per iteration (Ralph, `while :; do claude -p "$(cat PROMPT.md)"; done`, cron-driven agents, CI runners), the hook needs no per-iteration setup. `--install` writes it to `~/.claude/settings.json`, which is per-machine, so every fresh process on that machine inherits it. Points worth knowing:

- The **parent** model is still whatever the loop passes with `--model` (or your default). The hook only touches subagents the parent spawns.
- Each iteration's subagent fan-out gets routed the same way, so a loop that spawns a few `Explore` agents per pass stops paying the parent's rate for them.
- The audit log in `~/.claude/cheap_subagents.jsonl` accumulates across iterations. `--stats` after a long run shows the haiku/sonnet/parent split for the whole loop.
- For a loop pinned to one repo, `--install --project` scopes the hook to that repo's `.claude/settings.json` instead.

## Caveats

- The hook emits `permissionDecision: "allow"` because that's what makes `updatedInput` take effect. `Agent` spawns don't prompt for permission anyway, so this changes nothing else — but it does mean the hook, not your permission rules, is the last word on `Agent` calls.
- Heuristics are heuristics. If a task gets routed to haiku and the result is thin, re-spawn with `[model:opus]` and consider adding a pattern to `HEAVY_PATTERNS`.
- Hooks run inside subagents too. A subagent spawning its own subagent gets routed the same way.
- Requires Python 3.9+ on `PATH` (`sys.executable` at install time is recorded in settings).
- **Windows:** works without Git Bash or WSL. The installer records the full `sys.executable` path (e.g. `C:\Python312\python.exe`) and writes the hook in exec form (`command` + `args`), so Claude Code spawns the interpreter directly with no shell in between. Run `--install` from the same Python you want the hook to use.

## Tests

```bash
python3 tests/test_cheap_subagents.py
```

21 tests cover the decision table, the stdin/stdout hook contract, env overrides, log output, and installer idempotence.

## License

MIT
