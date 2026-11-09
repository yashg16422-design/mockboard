"""Nova's "Model produced invalid sequence as part of ToolUse" (Bedrock `ModelErrorException`).

Found live on Nov 9 by replaying the failed `repair` call: it is not a request problem, the
model's tool call could not be parsed. At temperature 0 with topK 1 an identical retry gives the
same broken call, so `structured` retries once with a short correction in the prompt."""

from __future__ import annotations

from typing import Any

import pytest
from botocore.exceptions import ClientError

from mockboard.brain.heard_repair import repairs_for
from mockboard.brain.llm import (
    BedrockNovaClient,
    LLMConfigError,
    LLMToolSequenceError,
    _map_boto_error,
    is_transient,
)
from mockboard.brain.prompts import PromptBlock
from mockboard.brain.tools import REPAIR_TOOL, RepairsOutput

SYSTEM = [PromptBlock("You find likely misheard words.")]
MESSAGE = ("Model produced invalid sequence as part of ToolUse. Please refer to the model tool "
           "use troubleshooting guide.")


def model_error(message: str = MESSAGE, code: str = "ModelErrorException") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, "Converse")


def repair_response(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "output": {"message": {"role": "assistant", "content": [
            {"toolUse": {"toolUseId": "t", "name": REPAIR_TOOL.name, "input": payload}}]}},
        "stopReason": "tool_use",
        "usage": {"inputTokens": 10, "outputTokens": 5},
    }


class Stub:
    def __init__(self, *items: Any) -> None:
        self.items = list(items)
        self.requests: list[dict[str, Any]] = []

    def converse(self, **request: Any) -> dict[str, Any]:
        self.requests.append(request)
        item = self.items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def nova(stub: Stub, **kw: Any) -> BedrockNovaClient:
    return BedrockNovaClient(region="ap-southeast-2", model_id="amazon.nova-pro-v1:0",
                             client=stub, **kw)


def test_the_broken_tool_sequence_has_its_own_error_type():
    err = _map_boto_error(model_error())
    assert isinstance(err, LLMToolSequenceError) and isinstance(err, LLMConfigError)
    assert not is_transient(err)
    other = _map_boto_error(model_error("Something else went wrong"))
    assert type(other) is LLMConfigError


async def test_one_retry_with_a_corrected_prompt_recovers():
    stats = []
    stub = Stub(model_error(), repair_response({"repairs": []}))
    out = await nova(stub, on_call=stats.append).structured(
        name="repair", system=SYSTEM, user="<answers>1. hello</answers>", tool=REPAIR_TOOL,
        output_model=RepairsOutput)
    assert out.repairs == []
    first, second = (r["messages"][0]["content"][0]["text"] for r in stub.requests)
    assert first == "<answers>1. hello</answers>"
    assert second.startswith(first) and "Call `record_repairs` exactly once" in second
    assert stats[0].attempts == 2 and stats[0].error is None


async def test_two_broken_sequences_raise_and_the_repair_step_shows_nothing():
    stats = []
    stub = Stub(model_error(), model_error())
    with pytest.raises(LLMToolSequenceError):
        await nova(stub, on_call=stats.append).structured(
            name="repair", system=SYSTEM, user="u", tool=REPAIR_TOOL, output_model=RepairsOutput)
    assert stats[0].attempts == 2 and stats[0].error == "LLMToolSequenceError"


async def test_repairs_for_swallows_the_error(settings):
    from dataclasses import replace

    from mockboard.brain.cards import builtin_test_cards
    from mockboard.brain.personas import load_personas
    from mockboard.brain.prompts import build_prompt_kit

    kit = build_prompt_kit(load_personas(), builtin_test_cards(), settings.prompt_versions())
    kit = replace(kit, versions={**kit.versions, "repair": settings.prompt_repair_version})
    stub = Stub(model_error(), model_error())
    assert await repairs_for(["main Almora se hoon"], llm=nova(stub), kit=kit) == []
    assert len(stub.requests) == 2
