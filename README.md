# ai-agents-orchestrator

A lightweight Python orchestrator that pipes tasks between **Claude Code** and **Gemini CLI**, enabling multi-agent pipelines with shared context.

## Requirements

- Python 3.14+
- [`claude`](https://claude.ai/code) CLI available on `PATH`
- [`gemini`](https://github.com/google-gemini/gemini-cli) CLI available on `PATH`
- [uv](https://github.com/astral-sh/uv) (for dependency management)

## Setup

```bash
uv sync
```

## Usage

```bash
python orchestrator.py <pipeline> <argument> [flags]
```

### Pipelines

| Pipeline | Argument | Description |
|---|---|---|
| `review-and-fix` | `<file>` | Gemini reviews the file, Claude implements the fixes |
| `research` | `<topic>` | Both agents research in parallel, returns combined findings |
| `research-and-implement` | `<topic>` | Parallel research, then Claude synthesizes and implements |
| `crossvalidate-and-implement` | `<topic>` | Gemini drafts a design, Claude validates and corrects it, Claude implements |

### Utility commands

| Command | Description |
|---|---|
| `info` | Show context file location and history entry count |
| `clear-context` | Delete the persisted context file |

### Flags

| Flag | Description |
|---|---|
| `--no-persist` | Run without loading or saving context between runs |
| `--timeout=SECONDS` | Per-agent timeout in seconds (default: 600) |

### Examples

```bash
# Have Gemini review a file and Claude fix the issues
python orchestrator.py review-and-fix src/main.py

# Research a topic using both agents in parallel
python orchestrator.py research "async Python patterns"

# Research and produce a working implementation
python orchestrator.py research-and-implement "rate limiter in Python"

# Gemini proposes a design, Claude validates and implements it
python orchestrator.py crossvalidate-and-implement "JWT authentication middleware"

# Run without persisting context
python orchestrator.py research "Redis caching" --no-persist

# Extend the timeout for long-running tasks
python orchestrator.py research-and-implement "distributed tracing" --timeout=1200
```

## How it works

### Context

Each pipeline shares a `Context` object that accumulates agent outputs as a history. By default the context is persisted to `.orchestrator_context.json` so subsequent runs build on prior results. Use `--no-persist` to opt out, or `clear-context` to reset.

### Primitives

- **`sequential(steps, ctx)`** — runs steps one after another; each step receives the previous step's output via `{output}` in the prompt template.
- **`parallel(tasks, ctx)`** — runs multiple agent calls concurrently and returns all results.

### Agent runners

- **`run_claude(prompt, ctx)`** — invokes `claude --dangerously-skip-permissions -p <prompt>`
- **`run_gemini(prompt, ctx)`** — invokes `gemini -p <prompt>`

Both prepend a truncated context summary to the prompt when history exists.

## Running tests

```bash
uv run pytest
```
