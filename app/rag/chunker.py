"""ตัดข้อความยาวเป็น chunk ขนาดพอดีสำหรับ embedding

ภาษาไทยไม่มีช่องว่างระหว่างคำ จึงตัดตามลำดับความสำคัญ:
ย่อหน้า -> บรรทัด -> เครื่องหมายวรรคตอน -> ช่องว่าง -> ตัวอักษร
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from .loaders import RawSection

_BREAKS = ["\n\n", "\n", "। ", "។ ", ". ", "! ", "? ", "; ", " ", ""]


@dataclass
class Chunk:
    id: str
    text: str
    source: str
    locator: str
    chunk_index: int

    def to_dict(self) -> dict:
        return asdict(self)


def _split_recursive(text: str, size: int, breaks: list[str]) -> list[str]:
    if len(text) <= size:
        return [text] if text.strip() else []

    sep = breaks[0]
    rest = breaks[1:] or [""]

    if sep == "":
        return [text[i : i + size] for i in range(0, len(text), size)]

    parts = text.split(sep)
    chunks: list[str] = []
    buf = ""
    for part in parts:
        candidate = f"{buf}{sep}{part}" if buf else part
        if len(candidate) <= size:
            buf = candidate
            continue
        if buf:
            chunks.append(buf)
        if len(part) > size:
            chunks.extend(_split_recursive(part, size, rest))
            buf = ""
        else:
            buf = part
    if buf:
        chunks.append(buf)
    return [c for c in chunks if c.strip()]


def _add_overlap(pieces: list[str], overlap: int) -> list[str]:
    """เติมท้ายของ chunk ก่อนหน้าไว้หัว chunk ถัดไป กันบริบทขาด"""
    if overlap <= 0 or len(pieces) < 2:
        return pieces
    out = [pieces[0]]
    for prev, cur in zip(pieces, pieces[1:]):
        out.append(prev[-overlap:].lstrip() + " " + cur)
    return out


def chunk_sections(
    sections: list[RawSection], chunk_size: int, chunk_overlap: int
) -> list[Chunk]:
    chunks: list[Chunk] = []
    counter: dict[str, int] = {}

    for section in sections:
        text = re.sub(r"\n{3,}", "\n\n", section.text).strip()
        if not text:
            continue

        pieces = _add_overlap(
            _split_recursive(text, chunk_size, _BREAKS), chunk_overlap
        )
        for piece in pieces:
            idx = counter.get(section.source, 0)
            counter[section.source] = idx + 1
            chunks.append(
                Chunk(
                    id=f"{section.source}#{idx}",
                    text=piece.strip(),
                    source=section.source,
                    locator=section.locator,
                    chunk_index=idx,
                )
            )
    return chunks
