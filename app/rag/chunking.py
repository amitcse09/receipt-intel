"""Split markdown policy docs into retrievable chunks.

Strategy: split on headings (each section is one self-contained rule), and only fall back to paragraph
splitting when a section is too long. Each chunk is prefixed with "Doc > Section" so it still makes
sense on its own once it's cut out of the document ("contextual chunk header").
"""

import hashlib
import re
from dataclasses import dataclass

MAX_CHARS = 1200  # ~300 tokens: small enough to be precise, big enough to hold a whole rule


@dataclass(frozen=True)
class Chunk:
    doc: str
    section: str
    content: str  # what gets embedded, searched and shown to the LLM (includes the header)

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.content.encode()).hexdigest()


def chunk_markdown(doc: str, text: str, max_chars: int = MAX_CHARS) -> list[Chunk]:
    title = doc
    sections: list[tuple[str, list[str]]] = []
    for line in text.splitlines():
        if m := re.match(r"^# (.+)", line):
            title = m.group(1).strip()
        elif m := re.match(r"^#{2,6} (.+)", line):
            sections.append((m.group(1).strip(), []))
        elif line.strip():
            if not sections:
                sections.append(("Overview", []))
            sections[-1][1].append(line.strip())

    chunks = []
    for section, lines in sections:
        header = f"{title} > {section}\n"
        body = " ".join(lines)
        for part in _split(body, max_chars - len(header)):
            chunks.append(Chunk(doc=doc, section=section, content=header + part))
    return chunks


def _split(body: str, limit: int) -> list[str]:
    """Greedy sentence packing; never cuts mid-sentence."""
    if len(body) <= limit:
        return [body]
    parts, current = [], ""
    for sentence in re.split(r"(?<=[.!?])\s+", body):
        if current and len(current) + 1 + len(sentence) > limit:
            parts.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        parts.append(current)
    return parts
