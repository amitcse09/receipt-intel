"""LLM-as-judge: grade what string matching can't — is every claim in the answer supported by the sources?

Caveats (say these in interviews): judges are themselves LLMs, so they can be wrong, prefer longer answers,
and favour their own model's style. Use a binary rubric (not a 1-10 score), ask for the evidence, keep the
judge prompt versioned, and spot-check its verdicts against human labels before trusting it.
"""

from pydantic import BaseModel

from app.llm.base import LLMClient

JUDGE_VERSION = "faithfulness-v1"

JUDGE_PROMPT = """You are a strict grader of a question-answering system.
Given SOURCES and an ANSWER, decide if EVERY factual claim in the ANSWER is directly supported by the SOURCES.
- Ignore style and length. Do not use outside knowledge.
- Arithmetic that correctly combines facts from the sources counts as supported.
- An answer that says it doesn't know is faithful.
- List each unsupported claim verbatim; empty list if none."""


class Verdict(BaseModel):
    faithful: bool
    unsupported_claims: list[str]


async def judge_faithfulness(sources: list[str], answer: str, llm: LLMClient) -> Verdict:
    blocks = "\n\n".join(f"<source>\n{s}\n</source>" for s in sources)
    prompt = f"<sources>\n{blocks}\n</sources>\n\n<answer>\n{answer}\n</answer>"
    return await llm.generate_structured(JUDGE_PROMPT, prompt, Verdict)
