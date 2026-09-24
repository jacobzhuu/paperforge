import json

import httpx
from llm_runtime.providers import OpenAICompatibleLLMProvider
from llm_runtime.tool_chat import ToolChatProvider
from llm_runtime.types import LLMRequest


def test_native_tool_messages_and_tool_only_completion():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "test",
                "model": "test",
                "usage": {"prompt_tokens": 8, "completion_tokens": 4},
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "c",
                                    "type": "function",
                                    "function": {"name": "compute_descriptive", "arguments": "{}"},
                                }
                            ],
                        },
                    }
                ],
            },
        )

    native = OpenAICompatibleLLMProvider(
        base_url="https://example.test/v1",
        api_key="test",
        model="test",
        timeout_seconds=5,
        max_retries=0,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    envelope = {
        "messages": [
            {"role": "system", "content": "scope"},
            {"role": "user", "content": "analyze"},
        ],
        "tools": [
            {
                "type": "function",
                "function": {"name": "compute_descriptive", "parameters": {"type": "object"}},
            }
        ],
    }
    result = ToolChatProvider(native).generate(
        LLMRequest(
            system_prompt="accounting",
            user_prompt=json.dumps(envelope),
            model="test",
            max_output_tokens=200,
        )
    )
    assert seen[0]["messages"] == envelope["messages"]
    assert seen[0]["tools"] == envelope["tools"]
    assert json.loads(result.text)["tool_calls"][0]["function"]["name"] == "compute_descriptive"
    assert result.usage["prompt_tokens"] == 8
