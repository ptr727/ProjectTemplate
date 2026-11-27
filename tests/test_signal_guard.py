"""The first signal decides, even when a later one starts inside the handler's first call."""

from __future__ import annotations

import importlib.util
import signal
import sys
import unittest
from pathlib import Path
from types import FrameType, ModuleType
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SITES = (
    "catalog/snippets/hub_fetch_run.py",
    "scripts/backlog_metrics.py",
    "scripts/canonical_review.py",
    "scripts/docker_lint.py",
    "scripts/pr_review.py",
    "host-setup/agent-safety/global_rules.py",
    "host-setup/agent-safety/claude/install.py",
)
for directory in ("scripts", "spec", "host-setup/agent-safety"):
    sys.path.insert(0, str(ROOT / directory))


def load(relative: str) -> ModuleType:
    path = ROOT / relative
    spec = importlib.util.spec_from_file_location(f"signal_guard_{path.stem}", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def nest_inside(on_signal: object, first: int, later: int) -> None:
    """Call `on_signal(later)` from the profile event of `on_signal(first)`'s own first call."""
    code = on_signal.__code__  # type: ignore[attr-defined]

    def nest(frame: FrameType, event: str, _arg: object) -> None:
        if event == "call" and frame.f_code is code:
            sys.setprofile(None)
            on_signal(later, frame)  # type: ignore[operator]

    sys.setprofile(nest)
    try:
        with unittest.TestCase().assertRaises(KeyboardInterrupt):
            on_signal(first, None)  # type: ignore[operator]
    finally:
        sys.setprofile(None)


class TestFirstSignalDecides(unittest.TestCase):
    def test_a_signal_nested_in_the_handler_is_passed_over(self) -> None:
        for relative in SITES:
            with self.subTest(site=relative):
                module = load(relative)
                nest_inside(module.on_signal, signal.SIGINT, signal.SIGTERM)
                self.assertEqual([signal.SIGINT], module.received)

    def test_a_signal_after_the_first_is_passed_over(self) -> None:
        for relative in SITES:
            with self.subTest(site=relative):
                module = load(relative)
                module.received.append(signal.SIGINT)
                module.on_signal(signal.SIGTERM, None)
                self.assertEqual([signal.SIGINT], module.received)

    def test_the_secrets_sync_handler_passes_over_a_nested_signal(self) -> None:
        module = load("scripts/secrets_sync.py")
        handlers: dict[int, object] = {}

        def main(_argv: object) -> int:
            nest_inside(handlers[signal.SIGTERM], signal.SIGINT, signal.SIGTERM)
            raise KeyboardInterrupt

        def record(signum: int, handler: object) -> None:
            handlers[signum] = handler

        with (
            mock.patch.object(module.signal, "signal", side_effect=record),
            mock.patch.object(module.signal, "getsignal", return_value=signal.default_int_handler),
            mock.patch.object(module, "main", side_effect=main),
            mock.patch("sys.stderr"),
        ):
            outcome = module.run([])
        self.assertEqual(signal.SIGINT, outcome)


if __name__ == "__main__":
    unittest.main()
