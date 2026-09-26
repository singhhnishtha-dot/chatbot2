#!/usr/bin/env python3
"""
Optional deliverable (challenge-brief.md §7.4): multi-turn reply handling.

This is a thin, spec-shaped wrapper around the same FSM that backs the
/v1/reply endpoint in bot.py (decide_reply). It exists as a separate file
so the multi-turn logic can be reviewed/tested independently of the HTTP
server, per the brief's suggested signature:

    def respond(state: ConversationState, merchant_message: str) -> dict

`state` here is a plain dict (no framework dependency) with the shape:
    {
        "history": [str, ...],           # prior merchant messages, oldest->newest
        "auto_reply_strikes": int,       # optional, default 0
        "last_topic": str,               # optional, e.g. the trigger kind that started this thread
    }
"""

from __future__ import annotations

from bot import decide_reply


def respond(state: dict, merchant_message: str) -> dict:
    """Given the conversation so far + the merchant's latest message, produce the reply.

    Returns one of:
        {"action": "send", "body": str, "cta": str, "rationale": str}
        {"action": "wait", "wait_seconds": int, "rationale": str}
        {"action": "end", "rationale": str, "body"?: str}
    and mutates `state["auto_reply_strikes"]` / appends to `state["history"]`
    in place so the caller can persist it for the next turn.
    """
    state = state or {}
    history = list(state.get("history", []))
    decision = decide_reply(history, merchant_message, {
        "auto_reply_strikes": state.get("auto_reply_strikes", 0),
        "last_topic": state.get("last_topic", "this"),
    })
    state.setdefault("history", []).append(merchant_message)
    if "_auto_reply_strikes" in decision:
        state["auto_reply_strikes"] = decision.pop("_auto_reply_strikes")
    return decision


if __name__ == "__main__":
    # Tiny smoke test / usage demo.
    convo_state = {"history": [], "last_topic": "renewal_due"}
    for msg in [
        "Thank you for contacting us! Our team will respond shortly.",
        "Thank you for contacting us! Our team will respond shortly.",
    ]:
        print(msg, "->", respond(convo_state, msg))

    convo_state2 = {"history": ["Tell me more first"], "last_topic": "active_planning_intent"}
    print("Ok lets do it ->", respond(convo_state2, "Ok lets do it, whats next?"))
