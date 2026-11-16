"""A narrow process trace, separate from official reward and semantic review."""

import re


def identity_trace(native_messages, user_id):
    """Use native conversation messages, which exclude injected memory contexts."""
    seen, assistant = [], []
    pattern = re.compile(r"(?<![\w])" + re.escape(user_id) + r"(?![\w])")
    for index, message in enumerate(native_messages):
        if message["role"] == "user" and pattern.search(message.get("content") or ""):
            seen.append(index)
        if message["role"] != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            arguments = call.get("arguments") or {}
            if "user_id" in arguments or "reservation_id" in arguments:
                return dict(status="literal_id_seen" if seen else "requires_review",
                    first_customer_data_tool_index=index, first_customer_data_tool=call["name"],
                    current_user_id_message_indices=seen, preceding_assistant_text_indices=assistant,
                    scope="literal_native_conversation_trace_not_authentication_or_policy_proof")
        if message.get("content"):
            assistant.append(index)
    return dict(status="no_customer_data_tool", first_customer_data_tool_index=None,
                current_user_id_message_indices=seen, preceding_assistant_text_indices=assistant,
                scope="literal_native_conversation_trace_not_authentication_or_policy_proof")
