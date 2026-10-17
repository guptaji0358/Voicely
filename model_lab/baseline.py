"""Deterministic rule baseline for the category task. A learned model must be compared against this, not against nothing.

It reuses the word lists of the application's own pre-synthesis analyser (speech_engine.text_analysis) so the baseline is what
the app already does today; it is deliberately NOT given the synthetic generator's trigger lists.
"""

from __future__ import annotations

import re

from speech_engine.text_analysis import ABBREVIATIONS, HETERONYMS, UNITS

_DATE = [re.compile(r"\b\d{4}-\d{2}-\d{2}\b"), re.compile(r"\b\d{1,2}[/.]\d{1,2}[/.]\d{2,4}\b"),
         re.compile(r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.? \d{1,2}(?:st|nd|rd|th)?(?:,? \d{4})?\b"),
         re.compile(r"\b\d{1,2} (?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*(?: \d{4})?\b")]
_NUM = re.compile(r"[$€£]?\d[\d,]*(?:\.\d+)?%?")
_UNIT = re.compile(r"\b\d[\d,.]*\s?([A-Za-z°]+)\b")
_ACR = re.compile(r"\b[A-Z]{2,6}\b")
_WORD = re.compile(r"[A-Za-z']+")


def predict(text: str) -> str:
    if any(p.search(text) for p in _DATE):
        return "date"
    m = _UNIT.search(text)
    if m and m.group(1).lower() in UNITS:
        return "unit"
    if _NUM.search(text):
        return "number"
    toks = text.lower().split()
    if any(t.strip(",;:!?") in ABBREVIATIONS for t in toks):
        return "abbreviation"
    if _ACR.search(text):
        return "acronym"
    if any(w.lower() in HETERONYMS for w in _WORD.findall(text)):
        return "heteronym"
    return "plain"
