"""Locate static document references without changing surrounding source text"""

from __future__ import annotations

import re
from html.parser import HTMLParser

_FENCE = re.compile(r"^[ \t]*(?P<fence>`{3,}|~{3,})(?P<info>.*)$")
_INLINE_CODE = re.compile(r"(?P<ticks>`+)(?!`)(.+?)(?<!`)(?P=ticks)(?!`)", re.DOTALL)
_MARKDOWN = re.compile(r"!?\[[^\]\n]*\]\(\s*(?:<(?P<angle>[^>\n]*)>|(?P<plain>[^\s)]+))")
_DEFINITION = re.compile(
    r"^ {0,3}\[[^\]\n]+\]:[ \t]*(?:\n[ \t]*)?(?:<(?P<angle>[^>\n]*)>|(?P<plain>\S+))",
    re.MULTILINE,
)
_ATTRIBUTE = re.compile(
    r"""(?P<name>[^\s/=>]+)(?:\s*=\s*(?:"(?P<double>[^"]*)"|'(?P<single>[^']*)'|(?P<bare>[^\s>]+)))?""",
    re.IGNORECASE,
)
_VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}


def source_reference_spans(content: str, *, html: bool = False) -> list[tuple[int, int]]:
    """
    Locate inline links, reference definitions, and HTML file attributes

    Return character offsets into the unchanged input. Protect displayed code
    while retaining raw HTML fences and indented content inside HTML blocks.
    Set `html` for complete HTML files, where Markdown code rules do not apply.
    """
    masked = list(content)
    raw_ranges: list[tuple[int, int]] = []
    indented_ranges: list[tuple[int, int]] = []
    offsets = [0]
    fence = ""
    raw = False
    for line in content.splitlines(keepends=True):
        start = offsets[-1]
        end = start + len(line)
        offsets.append(end)
        if html:
            continue
        match = _FENCE.match(line.rstrip("\r\n"))
        protect = False
        if fence:
            if raw:
                raw_ranges.append((start, end))
            protect = not raw
            stripped = line.strip()
            if stripped and set(stripped) == {fence[0]} and len(stripped) >= len(fence):
                fence = ""
                raw = False
                protect = True
        elif match:
            fence = match["fence"]
            raw = match["info"].strip() in {"{=html}", "{html}"}
            protect = True
        elif line.startswith(("    ", "\t")):
            indented_ranges.append((start, end))
        if protect:
            masked[start:end] = ["\n" if char == "\n" else " " for char in line]

    if not html:
        for match in _INLINE_CODE.finditer("".join(masked)):
            start, end = match.span()
            masked[start:end] = ["\n" if char == "\n" else " " for char in content[start:end]]
    visible = "".join(masked)
    spans: list[tuple[int, int]] = []
    tags: list[tuple[int, int]] = []
    displayed: list[tuple[int, int]] = []

    class References(HTMLParser):
        """HTML attribute locations outside displayed examples"""

        def __init__(self) -> None:
            super().__init__(convert_charrefs=False)
            self.stack: list[str] = []

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            line, column = self.getpos()
            start = offsets[line - 1] + column
            text = self.get_starttag_text()
            if text is None:
                return
            indented = any(left <= start < right for left, right in indented_ranges)
            in_raw = any(left <= start < right for left, right in raw_ranges)
            if indented and not self.stack and not in_raw:
                return
            tags.append((start, start + len(text)))
            if not {"pre", "code"}.intersection(self.stack):
                tag_end = re.match(r"<[^\s/>]+", text)
                assert tag_end is not None
                for attribute in _ATTRIBUTE.finditer(text, tag_end.end()):
                    if attribute["name"].lower() not in {"src", "href", "poster"}:
                        continue
                    name = next(
                        (
                            name
                            for name in ("double", "single", "bare")
                            if attribute[name] is not None
                        ),
                        None,
                    )
                    if name is None:
                        continue
                    left, right = attribute.span(name)
                    spans.append((start + left, start + right))
            if tag not in _VOID_TAGS:
                self.stack.append(tag)

        def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            depth = len(self.stack)
            self.handle_starttag(tag, attrs)
            del self.stack[depth:]

        def handle_endtag(self, tag: str) -> None:
            if tag in self.stack:
                index = len(self.stack) - 1 - self.stack[::-1].index(tag)
                del self.stack[index:]

        def handle_data(self, data: str) -> None:
            if {"pre", "code"}.intersection(self.stack):
                line, column = self.getpos()
                start = offsets[line - 1] + column
                displayed.append((start, start + len(data)))

    parser = References()
    parser.feed(visible)
    parser.close()
    if not html:
        for pattern in (_MARKDOWN, _DEFINITION):
            for match in pattern.finditer(visible):
                if any(
                    left <= match.start() < right
                    for left, right in indented_ranges + tags + displayed
                ):
                    continue
                name = "angle" if match["angle"] is not None else "plain"
                spans.append(match.span(name))
    return sorted(set(spans))
