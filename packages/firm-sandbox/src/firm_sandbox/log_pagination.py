"""Shared bounded log pagination for VM clients."""

from collections.abc import Callable, Iterator

PAGE_BYTES = 1024 * 1024


def pages(
    reader: Callable[..., bytes], run, offset: int = 0, until: int | None = None
) -> Iterator[bytes]:
    """Yield each non-empty log page from ``offset`` once, advancing by bytes read.

    Without ``until`` the empty page past the end stops the read. A caller that already
    knows the log's size passes it, and the read stops there without asking for that page.
    ``until`` also bounds each page's own request to the bytes remaining before it, so a
    log that grows after ``until`` was taken cannot push bytes past it into the result.
    """
    while until is None or offset < until:
        limit = PAGE_BYTES if until is None else min(PAGE_BYTES, until - offset)
        chunk = reader(run, offset=offset, limit=limit)
        if not chunk:
            return
        yield chunk
        offset += len(chunk)
