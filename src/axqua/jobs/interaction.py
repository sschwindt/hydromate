"""Answering, in a detached job, the questions an interactive study asks.

``convergence.run_mesh_convergence`` prompts twice: whether to reuse completed levels on
a re-run, and whether to refine one level further when the study has not converged. Its
default handler reads stdin with a timeout and, off a TTY, returns the default - so a
detached job does **not hang**, which is the important part.

But "does not hang" is not the same as "does the right thing": the default for *extend*
is no, so a study submitted specifically to march finer would quietly stop at its first
ladder. The fix is not to make the prompt smarter; it is to make the answer part of the
submission. :class:`PolicyAsk` answers from ``job.json``, never blocks, and records every
question and answer in ``runner.log`` so the decision is auditable rather than invisible.

An answer *file* is also honoured, which is what lets a future ``axqua answer JOB_ID
--yes`` reach a running study without restarting it. The file format is fixed here even
though the CLI verb is not built yet, so adding it later needs no migration.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

log = logging.getLogger("axqua.jobs.interaction")

__all__ = ["PolicyAsk", "classify"]

#: Prompt text is matched loosely on purpose. These are axqua's own prompts, so the
#: wording is ours to keep stable, but a substring match survives a reworded prompt where
#: an exact match would silently fall through to the default.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("resume", re.compile(r"\b(reuse|resume|completed level)", re.I)),
    ("extend", re.compile(r"\b(refine|extend|finer|another level)", re.I)),
)


def classify(prompt: str) -> str:
    """Map a prompt to a policy key, or ``"other"``."""
    for key, pattern in _PATTERNS:
        if pattern.search(prompt or ""):
            return key
    return "other"


class PolicyAsk:
    """A drop-in replacement for the study's ``ask`` callable.

    Signature-compatible with ``convergence._default_ask`` (``prompt``, keyword
    ``default``, and a tolerated ``timeout``), so it substitutes without the study
    knowing.
    """

    def __init__(self, answers: dict[str, bool] | None = None, *, sink: Any = None,
                 answer_file: Path | str | None = None, default: bool = False) -> None:
        self.answers = dict(answers or {})
        self.sink = sink
        self.answer_file = Path(answer_file) if answer_file else None
        self.default = bool(default)
        self.asked: list[tuple[str, bool, str]] = []

    def __call__(self, prompt: str, *, default: bool | None = None,
                 timeout: float | None = None) -> bool:      # noqa: ARG002
        key = classify(prompt)
        answer, source = self._answer(key, default)
        self.asked.append((prompt, answer, source))
        log.info("question %r -> %s (%s)", prompt, "yes" if answer else "no", source)
        if self.sink is not None:
            try:
                self.sink.decision(prompt, answer, source)
            except Exception:                        # noqa: BLE001 - pragma: no cover
                pass
        return answer

    def _answer(self, key: str, default: bool | None) -> tuple[bool, str]:
        # A file written since the job started wins: it is the most recent human intent.
        from_file = self._from_file(key)
        if from_file is not None:
            return from_file, "answers.json"
        if key in self.answers:
            return bool(self.answers[key]), "job.json options.answers"
        if default is not None:
            return bool(default), "the study's own default"
        return self.default, "the job default"

    def _from_file(self, key: str) -> bool | None:
        if self.answer_file is None or not self.answer_file.is_file():
            return None
        try:
            data = json.loads(self.answer_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.debug("cannot read %s: %s", self.answer_file, exc)
            return None
        if not isinstance(data, dict):
            return None
        value = data.get(key, data.get("all"))
        return None if value is None else bool(value)
