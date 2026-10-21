# cheap_subagents

**One file. No dependencies. Stops Claude Code from running every subagent on your most expensive model.**

If you've hit the "your subagent use is heavy, try cheaper models" message — or you just watch your Opus budget evaporate on `Explore` agents that grep for a filename — this is the fix. It's a single Python `PreToolUse` hook that intercepts every `Agent` call and sets `model` to `haiku` or `sonnet` unless the task actually needs the big model.

```
Explore / claude-code-guide / "find all usages of X"   →  haiku
general-purpose / custom agents / "implement Y"        →  sonnet
Plan / "security review" / "root cause" / "trade-offs" →  untouched (inherits parent, e.g. opus)
[model:opus] anywhere in the prompt                    →  forced, tag stripped
```

## Install

```bash
curl -fsSLO https://raw.githubusercontent.com/The-Guard305/cheap-subagents/main/cheap_subagents.py
mkdir -p ~/.claude && mv cheap_subagents.py ~/.claude/
python3 ~/.claude/cheap_subagents.py --install        # all projects (~/.claude/settings.json)
# or:  --install --project                              # just this repo (.claude/settings.json)
```

Restart Claude Code. `/hooks` should list an `Agent` matcher pointing at the script. That's it.

Uninstall: `python3 ~/.claude/cheap_subagents.py --uninstall`

## Proof it works

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
2. **Heavy signals** — regexes like `architect`, `trade-offs`, `root cause`, `security review`, `race condition` → leave `model` alone so the subagent inherits the parent.
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

## Caveats

- The hook emits `permissionDecision: "allow"` because that's what makes `updatedInput` take effect. `Agent` spawns don't prompt for permission anyway, so this changes nothing else — but it does mean the hook, not your permission rules, is the last word on `Agent` calls.
- Heuristics are heuristics. If a task gets routed to haiku and the result is thin, re-spawn with `[model:opus]` and consider adding a pattern to `HEAVY_PATTERNS`.
- Hooks run inside subagents too. A subagent spawning its own subagent gets routed the same way.
- Requires Python 3.9+ on `PATH` (`sys.executable` at install time is recorded in settings).

## Tests

```bash
python3 tests/test_cheap_subagents.py
```

20 tests cover the decision table, the stdin/stdout hook contract, env overrides, log output, and installer idempotence.

## License

MIT
