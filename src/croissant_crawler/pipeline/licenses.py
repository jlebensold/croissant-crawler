"""Normalise free-form license strings/URLs to short SPDX-style identifiers."""

from __future__ import annotations

import re

_PATTERNS: list[tuple[str, str]] = [
    (r"publicdomain/zero|cc0|cc-zero", "CC0-1.0"),
    (r"publicdomain/mark|public domain|^public$", "Public-Domain"),
    (r"by-nc-sa", "CC-BY-NC-SA"),
    (r"by-nc-nd", "CC-BY-NC-ND"),
    (r"by-nc", "CC-BY-NC"),
    (r"by-sa", "CC-BY-SA"),
    (r"by-nd", "CC-BY-ND"),
    (r"creativecommons\.org/licenses/by/|cc-by|cc by|attribution \d", "CC-BY"),
    (r"apache", "Apache-2.0"),
    (r"\bmit\b", "MIT"),
    (r"bsd-3|bsd 3", "BSD-3-Clause"),
    (r"bsd-2|bsd 2", "BSD-2-Clause"),
    (r"\bbsd\b", "BSD"),
    (r"agpl", "AGPL-3.0"),
    (r"lgpl", "LGPL"),
    (r"gpl-3|gpl 3|gpl3|gplv3", "GPL-3.0"),
    (r"gpl-2|gpl 2|gpl2|gplv2", "GPL-2.0"),
    (r"\bgpl\b", "GPL"),
    (r"odc-by|odc_by|licenses/by/1\.0|opendatacommons.org/licenses/by", "ODC-BY"),
    (r"odbl|database: open database|open database license", "ODbL"),
    (r"pddl", "PDDL"),
    (r"dbcl|database contents", "DbCL"),
    (r"openrail", "OpenRAIL"),
    (r"llama ?2", "Llama2"),
    (r"llama ?3", "Llama3"),
    (r"cdla-permissive", "CDLA-Permissive"),
    (r"cdla-sharing", "CDLA-Sharing"),
    (r"\bc-uda\b|computational use of data", "C-UDA"),
    (r"afl-3", "AFL-3.0"),
    (r"unlicense", "Unlicense"),
    (r"wtfpl", "WTFPL"),
    (r"\bother\b", "Other"),
    (r"unknown|not specified|^none$", "Unknown"),
]

_VERSION = re.compile(r"(\d\.\d)")


def normalize_license(raw: str | None) -> str:
    if not raw:
        return "Unknown"
    s = raw.strip().lower().replace("_", "-")
    for pattern, ident in _PATTERNS:
        if re.search(pattern, s):
            if ident.startswith("CC-BY"):
                m = _VERSION.search(s)
                if m:
                    return f"{ident}-{m.group(1)}"
            return ident
    return "Other"
