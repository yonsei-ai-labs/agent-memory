"""Common model transport errors and chat-completion envelopes."""

from agent_memory_eval.contracts.core import ContractError


class ProviderError(ContractError):
    def __init__(self, code, response=None, submitted=False, *, response_available=None):
        self.code, self.response, self.submitted = code, response, submitted
        self.response_available = response is not None if response_available is None else response_available
        super().__init__(code)


def assistant_message(response):
    """Return the first assistant message or a recordable error carrying the original body."""
    choices = response.get("choices") if isinstance(response, dict) else None
    choice = choices[0] if isinstance(choices, list) and choices else None
    message = choice.get("message") if isinstance(choice, dict) else None
    if not isinstance(message, dict) or message.get("role") != "assistant":
        raise ProviderError("INVALID_RESPONSE_SHAPE", response, submitted=True, response_available=True)
    return message
