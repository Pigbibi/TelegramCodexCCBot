"""Message splitting utility for Telegram's 4096-character limit.

Provides:
  - split_message(): splits long text into Telegram-safe chunks (≤4096 chars),
    preferring newline boundaries and preserving code block integrity.
"""

TELEGRAM_MAX_MESSAGE_LENGTH = 4096


def split_message(
    text: str, max_length: int = TELEGRAM_MAX_MESSAGE_LENGTH
) -> list[str]:
    """Split a message into chunks that fit Telegram's length limit.

    Tries to split on newlines when possible to preserve formatting.
    When a split occurs inside a fenced code block (```), the block is
    closed at the end of the current chunk and re-opened at the start
    of the next chunk so each chunk remains valid markdown.
    """
    if len(text) <= max_length:
        return [text]

    chunks: list[str] = []
    current_chunk = ""
    in_code_block = False
    code_fence = ""  # e.g. "```python"

    for line in text.split("\n"):
        stripped = line.strip()

        # Track code block state
        if stripped.startswith("```"):
            if not in_code_block:
                in_code_block = True
                code_fence = stripped  # remember "```lang"
            else:
                in_code_block = False

        # If single line exceeds max, split it forcefully
        if len(line) > max_length:
            if current_chunk:
                chunk_text = current_chunk.rstrip("\n")
                if in_code_block:
                    # The long line is inside a code block; close before flush
                    chunk_text += "\n```"
                chunks.append(chunk_text)
                current_chunk = (code_fence + "\n") if in_code_block else ""
            # Split long line into fixed-size pieces
            for i in range(0, len(line), max_length):
                chunks.append(line[i : i + max_length])
        elif len(current_chunk) + len(line) + 1 > max_length:
            # Current chunk is full, start a new one
            chunk_text = current_chunk.rstrip("\n")
            if in_code_block:
                chunk_text += "\n```"
            chunks.append(chunk_text)
            # Re-open code block in the new chunk
            if in_code_block:
                current_chunk = code_fence + "\n" + line + "\n"
            else:
                current_chunk = line + "\n"
        else:
            current_chunk += line + "\n"

    if current_chunk:
        chunks.append(current_chunk.rstrip("\n"))

    return chunks
