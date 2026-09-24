"""Native multi-turn tool conversation through the existing admission and cost ledger."""

import json
from dataclasses import replace


class ToolChatProvider:
    def __init__(self, provider):
        self.provider = provider

    def generate(self, request):
        envelope = json.loads(request.user_prompt)
        response = self.provider.generate(
            replace(
                request, messages=envelope["messages"], tools=envelope["tools"], json_output=False
            )
        )
        return replace(
            response, text=json.dumps({"text": response.text, "tool_calls": response.tool_calls})
        )
