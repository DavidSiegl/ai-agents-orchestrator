import unittest
import os
import json
import asyncio
import subprocess
from unittest.mock import patch, MagicMock, AsyncMock
from orchestrator import (
    Context, run_claude, run_gemini, sequential, parallel,
    review_and_fix, research, research_and_implement, crossvalidate_and_implement,
    main, TIMEOUT
)


class TestContext(unittest.TestCase):
    def setUp(self):
        self.test_file = ".test_context.json"
        if os.path.exists(self.test_file):
            os.remove(self.test_file)

    def tearDown(self):
        if os.path.exists(self.test_file):
            os.remove(self.test_file)

    def test_add(self):
        ctx = Context()
        ctx.add("agent1", "output1")
        self.assertEqual(len(ctx.history), 1)
        self.assertEqual(ctx.history[0], {
                         "agent": "agent1", "output": "output1"})

    def test_last(self):
        ctx = Context()
        self.assertIsNone(ctx.last())
        ctx.add("agent1", "output1")
        self.assertEqual(ctx.last(), "output1")
        ctx.add("agent2", "output2")
        self.assertEqual(ctx.last(), "output2")

    def test_summary(self):
        ctx = Context()
        ctx.add("agent1", "output1")
        ctx.add("agent2", "output2")
        expected = "[agent1]\noutput1\n\n[agent2]\noutput2"
        self.assertEqual(ctx.summary(), expected)

    def test_summary_truncation(self):
        ctx = Context()
        ctx.add("agent", "A" * 3000)
        ctx.add("agent", "B" * 2000)
        summary = ctx.summary(max_chars=1000)
        self.assertEqual(len(summary), 1000)
        self.assertTrue(summary.endswith("B" * 1000))

    def test_save_load(self):
        ctx = Context()
        ctx.add("agent1", "output1")
        ctx.artifacts["key"] = "value"
        ctx.save(self.test_file)

        self.assertTrue(os.path.exists(self.test_file))

        loaded = Context.load(self.test_file)
        self.assertEqual(loaded.history, ctx.history)
        self.assertEqual(loaded.artifacts, ctx.artifacts)

    def test_load_nonexistent(self):
        ctx = Context.load("nonexistent.json")
        self.assertEqual(ctx.history, [])
        self.assertEqual(ctx.artifacts, {})


class TestRunners(unittest.IsolatedAsyncioTestCase):
    @patch("orchestrator.subprocess.run")
    async def test_run_claude_success(self, mock_run):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = " Claude Output "
        mock_run.return_value = mock_result

        output = await run_claude("test prompt")

        self.assertEqual(output, "Claude Output")
        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        self.assertEqual(args[0], ["claude", "--dangerously-skip-permissions", "-p", "test prompt"])

    @patch("orchestrator.subprocess.run")
    async def test_run_claude_with_context(self, mock_run):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "ok"
        mock_run.return_value = mock_result

        ctx = Context()
        ctx.add("gemini", "previous output")

        await run_claude("new task", ctx)

        args, kwargs = mock_run.call_args
        full_prompt = args[0][3]
        self.assertIn("Previous steps:", full_prompt)
        self.assertIn("[gemini]\nprevious output", full_prompt)
        self.assertIn("Your task:\nnew task", full_prompt)

    @patch("orchestrator.subprocess.run")
    async def test_run_gemini_failure(self, mock_run):
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = " Error message "
        mock_run.return_value = mock_result

        with self.assertRaisesRegex(RuntimeError, "Gemini failed: Error message"):
            await run_gemini("test")

    @patch("orchestrator.subprocess.run")
    async def test_run_claude_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(["claude"], 600)
        with self.assertRaisesRegex(RuntimeError, "Claude timed out after 600s"):
            await run_claude("test")


class TestPrimitives(unittest.IsolatedAsyncioTestCase):
    @patch("orchestrator.run_claude", new_callable=AsyncMock)
    @patch("orchestrator.run_gemini", new_callable=AsyncMock)
    async def test_sequential(self, mock_gemini, mock_claude):
        mock_gemini.return_value = "gemini result"
        mock_claude.return_value = "claude result"

        ctx = Context()
        steps = [
            ("gemini", "step 1"),
            ("claude", "step 2 with {output}")
        ]

        await sequential(steps, ctx)

        self.assertEqual(len(ctx.history), 2)
        mock_gemini.assert_called_with("step 1", ctx)
        mock_claude.assert_called_with("step 2 with gemini result", ctx)

    @patch("orchestrator.run_claude", new_callable=AsyncMock)
    @patch("orchestrator.run_gemini", new_callable=AsyncMock)
    async def test_parallel(self, mock_gemini, mock_claude):
        mock_gemini.return_value = "gemini par"
        mock_claude.return_value = "claude par"

        ctx = Context()
        tasks = [
            ("gemini", "task 1"),
            ("claude", "task 2")
        ]

        results = await parallel(tasks, ctx)

        self.assertEqual(results, ["gemini par", "claude par"])
        self.assertEqual(len(ctx.history), 2)

    @patch("orchestrator.run_gemini", new_callable=AsyncMock)
    async def test_parallel_with_exception(self, mock_gemini):
        mock_gemini.side_effect = Exception("Boom")

        ctx = Context()
        tasks = [("gemini", "fail task")]

        results = await parallel(tasks, ctx)
        self.assertEqual(results, [""])
        self.assertEqual(len(ctx.history), 0)


class TestPipelines(unittest.IsolatedAsyncioTestCase):
    @patch("orchestrator.run_gemini", new_callable=AsyncMock)
    @patch("orchestrator.run_claude", new_callable=AsyncMock)
    async def test_review_and_fix(self, mock_claude, mock_gemini):
        mock_gemini.return_value = "issues json"
        mock_claude.return_value = "fixed code"

        with patch("builtins.open", unittest.mock.mock_open(read_data="original code")):
            result = await review_and_fix("dummy.py", persist=False)

        self.assertEqual(result, "fixed code")
        mock_gemini.assert_called_once()
        mock_claude.assert_called_once()

    @patch("orchestrator.run_gemini", new_callable=AsyncMock)
    @patch("orchestrator.run_claude", new_callable=AsyncMock)
    async def test_research(self, mock_claude, mock_gemini):
        mock_gemini.return_value = "res1"
        mock_claude.return_value = "res2"

        result = await research("topic", persist=False)
        self.assertIn("[gemini]\nres1", result)
        self.assertIn("[claude]\nres2", result)

    @patch("orchestrator.parallel", new_callable=AsyncMock)
    @patch("orchestrator.run_claude", new_callable=AsyncMock)
    async def test_research_and_implement(self, mock_claude, mock_parallel):
        mock_parallel.return_value = ["res1", "res2"]
        mock_claude.return_value = "impl"

        result = await research_and_implement("topic", persist=False)
        self.assertEqual(result, "impl")
        mock_parallel.assert_called_once()
        mock_claude.assert_called_once()

    @patch("orchestrator.run_gemini", new_callable=AsyncMock)
    @patch("orchestrator.run_claude", new_callable=AsyncMock)
    async def test_crossvalidate_and_implement(self, mock_claude, mock_gemini):
        mock_gemini.return_value = "draft"
        mock_claude.side_effect = ["validated", "implementation"]

        result = await crossvalidate_and_implement("topic", persist=False)
        self.assertEqual(result, "implementation")
        self.assertEqual(mock_claude.call_count, 2)


class TestCLI(unittest.TestCase):
    def setUp(self):
        self.orig_timeout = TIMEOUT

    def tearDown(self):
        import orchestrator
        orchestrator.TIMEOUT = self.orig_timeout

    @patch("sys.exit")
    @patch("builtins.print")
    def test_main_no_args(self, mock_print, mock_exit):
        main([])
        mock_exit.assert_called_with(1)
        mock_print.assert_any_call(
            "Usage: orchestrator.py <pipeline> [arg] [--no-persist] [--timeout=SECONDS]")

    @patch("sys.exit")
    @patch("orchestrator.Context.load")
    @patch("builtins.print")
    def test_main_info(self, mock_print, mock_load, mock_exit):
        mock_load.return_value = Context()
        main(["info"])
        mock_exit.assert_called_with(0)
        mock_print.assert_any_call("context entries: 0")

    @patch("sys.exit")
    @patch("os.path.exists")
    @patch("os.remove")
    @patch("builtins.print")
    def test_main_clear_context(self, mock_print, mock_remove, mock_exists, mock_exit):
        mock_exists.return_value = True
        main(["clear-context"])
        mock_remove.assert_called_once()
        mock_print.assert_any_call("Context cleared.")
        mock_exit.assert_called_with(0)

    @patch("orchestrator.asyncio.run")
    @patch("orchestrator.research", new_callable=AsyncMock)
    @patch("builtins.print")
    def test_main_pipeline_dispatch(self, mock_print, mock_research, mock_asyncio_run):
        mock_research.return_value = "result"
        # Make asyncio.run return whatever the coroutine would have returned

        def side_effect(coro):
            if asyncio.iscoroutine(coro):
                coro.close()
            return "result"
        mock_asyncio_run.side_effect = side_effect

        main(["research", "my topic"])

        mock_research.assert_called_with("my topic", persist=True)
        mock_print.assert_any_call("result")

    @patch("sys.exit")
    def test_main_timeout_flag(self, mock_exit):
        import orchestrator
        main(["--timeout=123", "info"])
        self.assertEqual(orchestrator.TIMEOUT, 123)


if __name__ == "__main__":
    unittest.main()
