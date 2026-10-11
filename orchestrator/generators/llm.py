"""A live coding agent: one model call per candidate, each with a different strategy, in parallel."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re

from catalog import TargetInfo
from models import Candidate, CandidateFile

from generators.base import CandidateGenerator

logger = logging.getLogger("ces.llm")

STRATEGIES = [
    ("smallest-fix", "Make the smallest possible change that makes every test pass."),
    ("root-cause", "Find the root cause of each failing assertion and fix it without changing the public API."),
    ("integer-math", "Use only integer arithmetic. Avoid floating point and rounding helpers."),
    ("defensive", "Fix the bugs and also harden edge cases such as overflow and invalid arguments."),
    ("readable-rewrite", "Rewrite the affected method for clarity while keeping behavior and API identical."),
    ("trace-the-tests", "Walk through each failing test by hand, computing expected values step by step, then edit."),
]

SYSTEM_PROMPT = """You are a coding agent inside an automated repair loop.
A referee compiles and tests every candidate you produce in an isolated sandbox that has no network access.
Rules:
- Return the COMPLETE new contents of every file you change.
- Only change the files listed as editable. Changing tests or build files gets the candidate disqualified.
- Keep the public API unchanged.
- Do not add logging, telemetry, or network calls."""

RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["strategy", "explanation", "files"],
    "properties": {
        "strategy": {"type": "string", "description": "A short name for your approach (3 to 6 words)."},
        "explanation": {"type": "string", "description": "One or two sentences on what you changed and why."},
        "files": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["path", "content"],
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            },
        },
    },
}


def build_prompt(target: TargetInfo, approach: str, feedback: str | None) -> str:
    parts = [f"# Issue\n{target.issue}", "# Editable files"]
    parts += [f"## {path}\n```\n{target.read(path)}\n```" for path in target.editable]
    parts.append("# Read-only context (do not modify)")
    parts += [f"## {path}\n```\n{target.read(path)}\n```" for path in target.context]
    parts.append(f"# Your approach\n{approach}")
    if feedback:
        parts.append("# What happened to the previous wave\n"
                     "The referee rejected every earlier candidate. Learn from these results:\n" + feedback)
    return "\n\n".join(parts)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:32] or "candidate"


class LlmGenerator(CandidateGenerator):
    name = "llm"

    def __init__(self, endpoint: str, deployment: str):
        from azure.identity.aio import get_bearer_token_provider
        from azure_auth import async_credential
        from openai import AsyncOpenAI

        self.deployment = deployment
        token_provider = get_bearer_token_provider(async_credential(), "https://cognitiveservices.azure.com/.default")
        # Keyless: the Entra token provider is passed where an API key would go.
        self.client = AsyncOpenAI(base_url=endpoint.rstrip("/") + "/openai/v1/", api_key=token_provider,
                                  timeout=180, max_retries=2)
        self.reasoning_effort = os.environ.get("LLM_REASONING_EFFORT", "").strip()

    async def generate(self, target: TargetInfo, *, wave: int, count: int, feedback: str | None) -> list[Candidate]:
        offset = (wave - 1) * count
        approaches = [STRATEGIES[(offset + i) % len(STRATEGIES)] for i in range(count)]
        results = await asyncio.gather(*(self._one(target, wave, index, key, approach, feedback)
                                         for index, (key, approach) in enumerate(approaches)),
                                       return_exceptions=True)
        candidates = [result for result in results if isinstance(result, Candidate)]
        for result in results:
            if isinstance(result, Exception):
                logger.warning("A candidate request failed: %s", result)
        if not candidates:
            first_error = next((r for r in results if isinstance(r, Exception)), None)
            raise RuntimeError(f"The coding agent produced no candidates: {first_error}")
        return candidates

    async def _one(self, target: TargetInfo, wave: int, index: int, key: str, approach: str,
                   feedback: str | None) -> Candidate:
        extra = {"reasoning_effort": self.reasoning_effort} if self.reasoning_effort else {}
        response = await self.client.chat.completions.create(
            model=self.deployment,
            messages=[{"role": "system", "content": SYSTEM_PROMPT},
                      {"role": "user", "content": build_prompt(target, approach, feedback)}],
            response_format={"type": "json_schema",
                             "json_schema": {"name": "candidate_fix", "strict": True, "schema": RESPONSE_SCHEMA}},
            **extra,
        )
        data = json.loads(response.choices[0].message.content or "{}")
        files = [CandidateFile(path=f["path"].strip().removeprefix("./"), content=f["content"])
                 for f in data.get("files", [])]
        return Candidate(
            id=f"w{wave}-{index + 1}-{_slug(key)}",
            wave=wave,
            strategy=data.get("strategy") or key,
            author=self.deployment,
            explanation=data.get("explanation", ""),
            files=files,
        )
