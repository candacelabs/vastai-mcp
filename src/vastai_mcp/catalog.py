"""Offline source and signature lookup, including approximate symbol matches."""

from difflib import SequenceMatcher
from importlib.resources import files
import json
import re


INDEX = json.loads(files(__package__).joinpath("sdk-index.json").read_text(encoding="utf-8"))
SYMBOLS = {row["name"]: row for row in INDEX["symbols"]}


def source(row):
    return f"https://github.com/vast-ai/vast-cli/blob/{INDEX['revision']}/{row['path']}#L{row['line']}"


def search(query, limit):
    words = re.findall(r"[a-z0-9]+", query.lower())
    if not words:
        raise ValueError("Enter a symbol or search terms.")
    needle = " ".join(words)
    ranked = []
    for name, row in SYMBOLS.items():
        short = name.rsplit(".", 1)[-1].lower().replace("_", " ")
        normalized = name.lower().replace("_", " ").replace(".", " ")
        doc = row["doc"].lower()
        score = max(
            1.0 if needle in (short, normalized) else 0,
            0.94 if normalized.endswith(" " + needle) else 0,
            0.84 * sum(w in short.split() for w in words) / len(words),
            0.78 * SequenceMatcher(None, needle, short).ratio(),
            0.65 * sum(w in normalized.split() or w in doc for w in words) / len(words),
        )
        if score >= 0.48:
            ranked.append((-score, not name.startswith("vastai.sdk.VastAI."), name))
    return [{"name": name, "summary": SYMBOLS[name]["doc"].partition("\n")[0], "source": source(SYMBOLS[name])}
            for _, _, name in sorted(ranked)[:limit]]


def describe(name):
    name = name.strip()
    if name == "vastai.VastAI" or name.startswith("vastai.VastAI."):
        name = "vastai.sdk." + name.removeprefix("vastai.")
    matches = [row for key, row in SYMBOLS.items() if key == name or key.endswith("." + name)]
    if len(matches) != 1:
        detail = ", ".join(row["name"] for row in matches[:6]) or "use sdk_search to find it"
        raise ValueError(f"Expected one SDK symbol for {name!r}: {detail}")
    row = matches[0]
    return {**row, "source": source(row), "revision": INDEX["revision"]}
