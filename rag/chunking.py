"""chunking.py — split long documents into overlapping character windows."""


def chunk_text(text: str, size: int = 1000, overlap: int = 150) -> list[str]:
    """
    Split text into ~`size`-char chunks with `overlap` chars of context
    carried between consecutive chunks. Splits on paragraph boundaries when
    possible to keep chunks semantically coherent.
    """
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]

    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    buf = ""

    for para in paragraphs:
        if len(buf) + len(para) + 2 <= size:
            buf = f"{buf}\n\n{para}" if buf else para
        else:
            if buf:
                chunks.append(buf)
            if len(para) <= size:
                buf = para
            else:
                # Hard-split an oversized paragraph with overlap.
                start = 0
                while start < len(para):
                    chunks.append(para[start:start + size])
                    start += size - overlap
                buf = ""

    if buf:
        chunks.append(buf)

    return chunks
