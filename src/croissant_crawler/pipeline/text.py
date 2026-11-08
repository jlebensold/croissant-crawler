"""Plain-text helpers: turn README-style Markdown/HTML descriptions into readable text."""

from __future__ import annotations

import html
import re

_PATTERNS = [
    (re.compile(r"<!--.*?-->", re.S), " "),
    (re.compile(r"```.*?```", re.S), " "),                     # fenced code
    (re.compile(r"!\[[^\]]*\]\([^)]*\)"), " "),                # images
    (re.compile(r"!\[[^\]]*\]\[[^\]]*\]"), " "),               # reference images
    (re.compile(r"\[([^\]]+)\]\([^)]*\)"), r"\1"),             # links -> text
    (re.compile(r"\[([^\]]+)\]\[[^\]]*\]"), r"\1"),            # reference links -> text
    (re.compile(r"^\s*\[[^\]]+\]:\s*\S+.*$", re.M), " "),      # reference definitions
    (re.compile(r"<[^>]{1,300}>"), " "),                       # html tags
    (re.compile(r"^\s{0,3}#{1,6}\s*", re.M), ""),              # headings
    (re.compile(r"^\s*([-*_]\s*){3,}$", re.M), " "),           # rules
    (re.compile(r"^\s*\|?[-:\s|]{3,}\|?\s*$", re.M), " "),     # table separators
    (re.compile(r"(\*\*|__|\*|`)"), ""),                       # emphasis / code ticks
    (re.compile(r"^\s*>\s?", re.M), ""),                       # blockquotes
    (re.compile(r"^\s*[-*+]\s+", re.M), "• "),                 # bullets
]


def markdown_to_text(md: str, limit: int | None = None) -> str:
    if not md:
        return ""
    text = html.unescape(md)
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"\s*\n\s*", "\n", text)
    text = re.sub(r"\n{2,}", "\n", text).strip()
    text = text.replace("\n", " · ") if limit and limit < 2000 else text
    if limit and len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "…"
    return text
