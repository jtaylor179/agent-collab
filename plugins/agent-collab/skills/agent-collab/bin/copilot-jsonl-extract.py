#!/usr/bin/env python3
"""Extract one final Copilot assistant response from a JSONL transport stream.

The assistant content is opaque: this validates only Copilot's transport
envelopes and writes the selected string verbatim. It never parses, normalizes,
repairs, or otherwise interprets the content itself.
"""
from __future__ import annotations

import json
import sys


def extract_assistant_content(raw_lines):
    final_messages = []
    for line_number, raw_line in enumerate(raw_lines, 1):
        if not raw_line.strip():
            continue
        try:
            line = raw_line.decode("utf-8")
            envelope = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"malformed Copilot JSONL envelope on line {line_number}: {exc}"
            ) from exc
        if not isinstance(envelope, dict):
            raise ValueError(
                f"Copilot JSONL envelope on line {line_number} is not an object"
            )
        if envelope.get("type") != "assistant.message":
            continue
        data = envelope.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("content"), str):
            raise ValueError(
                f"assistant.message on line {line_number} has non-string data.content"
            )
        tool_requests = data.get("toolRequests", [])
        if not isinstance(tool_requests, list):
            raise ValueError(
                f"assistant.message on line {line_number} has invalid data.toolRequests"
            )
        # Tool-request and subagent messages are legitimate intermediate events.
        # Copilot's final top-level response has neither.
        if tool_requests or data.get("parentToolCallId") is not None:
            continue
        final_messages.append(data["content"])

    if len(final_messages) != 1:
        raise ValueError(
            "Copilot JSONL must contain exactly one final assistant.message; "
            f"found {len(final_messages)}"
        )
    return final_messages[0]


def main():
    try:
        content = extract_assistant_content(sys.stdin.buffer)
        encoded = content.encode("utf-8")
    except (UnicodeEncodeError, ValueError) as exc:
        print(f"copilot transport error: {exc}", file=sys.stderr)
        return 2
    sys.stdout.buffer.write(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
