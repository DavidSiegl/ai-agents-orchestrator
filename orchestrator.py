"""
Agent orchestrator — pipes tasks between Claude Code and Gemini CLI.
"""

import asyncio
import subprocess
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any

CONTEXT_FILE = os.path.join(os.path.dirname(
    __file__), ".orchestrator_context.json")


# ---------------------------------------------------------------------------
# Context store
# ---------------------------------------------------------------------------

@dataclass
class Context:
    """Shared state passed between agents across pipeline steps."""
    history: list[dict] = field(default_factory=list)
    artifacts: dict[str, Any] = field(default_factory=dict)

    def add(self, agent: str, output: str) -> None:
        self.history.append({"agent": agent, "output": output})

    def last(self) -> str | None:
        return self.history[-1]["output"] if self.history else None

    def summary(self, max_chars: int = 4000) -> str:
        """Truncated history to stay within context limits."""
        combined = "\n\n".join(
            f"[{h['agent']}]\n{h['output']}" for h in self.history
        )
        return combined[-max_chars:] if len(combined) > max_chars else combined

    def save(self, path: str = CONTEXT_FILE) -> None:
        with open(path, "w") as f:
            json.dump({"history": self.history,
                      "artifacts": self.artifacts}, f, indent=2)

    @classmethod
    def load(cls, path: str = CONTEXT_FILE) -> "Context":
        if not os.path.exists(path):
            return cls()
        with open(path) as f:
            data = json.load(f)
        return cls(history=data.get("history", []), artifacts=data.get("artifacts", {}))


# ---------------------------------------------------------------------------
# Agent runners
# ---------------------------------------------------------------------------

TIMEOUT = 600
MAX_PHASE_INPUT_CHARS = 3000


async def run_claude(prompt: str, ctx: Context | None = None) -> str:
    """
    Call Claude Code in print mode.
    Prepends context history so Claude knows what happened before.
    """
    full_prompt = prompt
    if ctx and ctx.history:
        full_prompt = f"Previous steps:\n{
            ctx.summary()}\n\nYour task:\n{prompt}"

    try:
        result = await asyncio.to_thread(
            subprocess.run,
            ["claude", "--dangerously-skip-permissions", "-p", full_prompt],
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Claude timed out after {TIMEOUT}s")
    if result.returncode != 0:
        raise RuntimeError(f"Claude failed: {result.stderr.strip()}")
    return result.stdout.strip()


async def run_gemini(prompt: str, ctx: Context | None = None) -> str:
    """
    Call Gemini CLI in print mode.
    Same context injection pattern as Claude.
    """
    full_prompt = prompt
    if ctx and ctx.history:
        full_prompt = f"Previous steps:\n{
            ctx.summary()}\n\nYour task:\n{prompt}"

    try:
        result = await asyncio.to_thread(
            subprocess.run,
            ["gemini", "-p", full_prompt],
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Gemini timed out after {TIMEOUT}s")
    if result.returncode != 0:
        raise RuntimeError(f"Gemini failed: {result.stderr.strip()}")
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# Pipeline primitives
# ---------------------------------------------------------------------------

async def sequential(
    steps: list[tuple[str, str]],
    ctx: Context,
    labels: dict[str, str] | None = None,
) -> Context:
    """
    Run steps one after another, each step seeing the previous output.

    steps: list of (agent, prompt_template) where {output} in the template
           is replaced with the previous step's output.
    labels: optional display label per agent, e.g. {"gemini": "[gemini] reviewing"}
    """
    runners = {"claude": run_claude, "gemini": run_gemini}

    for agent, prompt_tpl in steps:
        prev = ctx.last() or ""
        prompt = prompt_tpl.format(output=prev)
        label = (labels or {}).get(agent, f"[{agent}]")
        output = await runners[agent](prompt, ctx)
        ctx.add(agent, output)
        print(f"  {label} done ({len(output)} chars)", file=sys.stderr)

    return ctx


async def parallel(
    tasks: list[tuple[str, str]],
    ctx: Context,
    label: str = "agents",
) -> list[str]:
    """
    Run multiple agent calls at the same time, return all results.
    Useful when two agents can independently analyze something.
    """
    runners = {"claude": run_claude, "gemini": run_gemini}
    coros = [runners[agent](prompt, ctx) for agent, prompt in tasks]
    results = await asyncio.gather(*coros, return_exceptions=True)

    outputs = []
    for (agent, _), result in zip(tasks, results):
        if isinstance(result, Exception):
            print(f"  [{agent}] failed: {result}", file=sys.stderr)
            outputs.append("")
        else:
            ctx.add(agent, result)
            outputs.append(result)
    return outputs


# ---------------------------------------------------------------------------
# Example pipelines
# ---------------------------------------------------------------------------

async def review_and_fix(file_path: str, persist: bool = True) -> str:
    """
    Gemini reviews the code, Claude implements the fixes.
    Classic two-agent handoff.
    """
    ctx = Context.load() if persist else Context()

    with open(file_path) as f:
        code = f.read()

    ctx = await sequential(
        [
            ("gemini", f"Review this code and list the top 3 issues as JSON:\n```\n{
             code}\n```"),
            ("claude",
             "Implement fixes for these issues:\n{output}\n\nOriginal code:\n```\n" + code + "\n```"),
        ],
        ctx,
        labels={"gemini": "[gemini] reviewing", "claude": "[claude] fixing"},
    )

    if persist:
        ctx.save()
    return ctx.last()


async def research(topic: str, persist: bool = True) -> str:
    """
    Both agents research independently in parallel and return the combined findings.
    No synthesis or implementation step.
    """
    ctx = Context.load() if persist else Context()

    await parallel(
        [
            ("gemini", f"Find recent best practices and examples for: {topic}"),
            ("claude",
             f"Describe the technical approach and tradeoffs for: {topic}"),
        ],
        ctx,
        label="[gemini + claude] researching",
    )
    print("  [gemini + claude] research done", file=sys.stderr)

    if persist:
        ctx.save()
    return ctx.summary()


async def crossvalidate_and_implement(topic: str, persist: bool = True) -> str:
    """
    Gemini proposes a solution, Claude validates and corrects it, then implements the final version.

    Phase 1 — Gemini drafts a solution design.
    Phase 2 — Claude critiques it, identifies flaws, and produces a corrected design.
    Phase 3 — Claude implements the validated (and possibly corrected) design.
    """
    ctx = Context.load() if persist else Context()

    # Phase 1: Gemini produces the initial solution design
    # ctx not passed — prompts are self-contained, passing ctx would duplicate content via summary injection
    draft = await run_gemini(
        f"Propose a detailed solution design for the following task. "
        f"Include architecture decisions, edge cases, and potential pitfalls:\n\n{topic}",
    )
    ctx.add("gemini", draft)
    print("  [gemini] draft ready", file=sys.stderr)

    # Phase 2: Claude validates and fixes the draft
    validated = await run_claude(
        f"You are a critical reviewer. Examine the following solution design:\n\n{draft}\n\n"
        f"1. Identify any correctness issues, missing edge cases, or design flaws.\n"
        f"2. Produce a corrected and improved solution design, incorporating your fixes.\n"
        f"Output only the final corrected design.",
    )
    ctx.add("claude", validated)
    print("  [claude] validation done", file=sys.stderr)

    # Phase 3: Claude implements the validated design
    # Truncate input and constrain output scope to stay within what a single claude -p call can produce
    validated_input = validated[-MAX_PHASE_INPUT_CHARS:] if len(validated) > MAX_PHASE_INPUT_CHARS else validated
    implementation = await run_claude(
        f"Implement the following validated solution design as concise, production-ready code. "
        f"Cover the core cases only — do not generate exhaustive edge-case coverage or lengthy docstrings. "
        f"Output only the code, no explanation:\n\n{validated_input}",
    )
    ctx.add("claude", implementation)
    print("  [claude] implementation done", file=sys.stderr)

    if persist:
        ctx.save()
    return ctx.last()


async def research_and_implement(topic: str, persist: bool = True) -> str:
    """
    Both agents research independently, then Claude synthesizes + implements.
    """
    ctx = Context.load() if persist else Context()

    # Phase 1: parallel research
    await parallel(
        [
            ("gemini", f"Find recent best practices and examples for: {topic}"),
            ("claude",
             f"Describe the technical approach and tradeoffs for: {topic}"),
        ],
        ctx,
        label="[gemini + claude] researching",
    )
    print("  [gemini + claude] research done", file=sys.stderr)

    # Phase 2: synthesis
    synthesis_prompt = (
        f"Based on this research:\n\n{ctx.summary()}\n\n"
        f"Write a production-ready implementation for: {topic}"
    )
    output = await run_claude(synthesis_prompt, ctx)
    ctx.add("claude", output)
    print("  [claude] synthesis done", file=sys.stderr)

    if persist:
        ctx.save()
    return ctx.last()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main(args: list[str]) -> None:
    global TIMEOUT
    persist = "--no-persist" not in args
    args = [a for a in args if a != "--no-persist"]

    timeout_arg = next((a for a in args if a.startswith("--timeout=")), None)
    if timeout_arg:
        TIMEOUT = int(timeout_arg.split("=", 1)[1])
        args = [a for a in args if not a.startswith("--timeout=")]

    if not args:
        print("Usage: orchestrator.py <pipeline> [arg] [--no-persist] [--timeout=SECONDS]")
        print("  pipelines: review-and-fix, research, research-and-implement, crossvalidate-and-implement")
        print("  utility:   info, clear-context")
        print("  flags:     --no-persist       run without loading or saving context")
        print("             --timeout=SECONDS  per-agent timeout (default: 600)")
        sys.exit(1)
        return

    pipeline = args[0]

    if pipeline == "info":
        ctx = Context.load()
        print(f"script dir:   {os.path.dirname(os.path.abspath(__file__))}")
        print(f"working dir:  {os.getcwd()}")
        print(f"context file: {CONTEXT_FILE}")
        print(f"context exists: {os.path.exists(CONTEXT_FILE)}")
        print(f"context entries: {len(ctx.history)}")
        sys.exit(0)
        return

    if pipeline == "clear-context":
        if os.path.exists(CONTEXT_FILE):
            os.remove(CONTEXT_FILE)
            print("Context cleared.")
        else:
            print("No context file found.")
        sys.exit(0)
        return

    if len(args) < 2:
        print(f"Pipeline '{pipeline}' requires an argument.")
        sys.exit(1)
        return

    arg = args[1]

    match pipeline:
        case "review-and-fix":
            result = asyncio.run(review_and_fix(arg, persist=persist))
        case "research":
            result = asyncio.run(research(arg, persist=persist))
        case "research-and-implement":
            result = asyncio.run(research_and_implement(arg, persist=persist))
        case "crossvalidate-and-implement":
            result = asyncio.run(
                crossvalidate_and_implement(arg, persist=persist))
        case _:
            print(f"Unknown pipeline: {pipeline}")
            sys.exit(1)
            return

    print(result)


if __name__ == "__main__":
    main(sys.argv[1:])
