"""An independent second look at the final patch."""
from __future__ import annotations

import re
from typing import List, Tuple

from .llm import LLMClient, LLMError, Usage
from .prompts import REVIEW
from .toolparse import parse_arguments


def review(llm: LLMClient, issue: str, diff: str, evidence: str) -> Tuple[bool, List[str], Usage]:
    prompt = REVIEW.format(issue=issue[:8000], diff=diff[:24000], evidence=evidence[:4000])
    try:
        reply = llm.chat([{"role": "user", "content": prompt}], tools=None, max_tokens=4096)
    except LLMError:
        return False, ["the independent reviewer could not complete its check"], Usage()
    text = reply.content
    match = re.search(r"\{.*\}", text, re.S)
    obj, err = parse_arguments(match.group(0) if match else text)
    if err:
        return False, ["the independent reviewer returned an unreadable result"], reply.usage
    verdict = str(obj.get("verdict", "")).lower()
    if verdict not in ("approve", "revise"):
        return False, ["the independent reviewer did not return a valid verdict"], reply.usage
    problems = [str(p) for p in obj.get("problems") or [] if str(p).strip()]
    approved = verdict == "approve" and not problems
    if verdict == "revise" and not problems:
        problems = ["the independent reviewer requested revision without explaining why"]
    return approved, problems, reply.usage
