"""Synthetic labelled data for the initial task, "which pronunciation-risk category does this text segment contain?".

PROVENANCE (be clear about it): these labels are correct BY CONSTRUCTION - each segment is assembled from neutral filler words plus
exactly one trigger whose category is known - they are not user data, not production data, and not produced by any model.
A model trained on them shows that the training/evaluation machinery works and that the category is learnable on this synthetic
distribution; it says nothing about quality on real documents. Real approved corrections (feedback.py) are added through
data.import_feedback_records when they exist.

Label priority when a segment has several triggers does not matter here: every segment has exactly one trigger.
The ``heldout`` pools are kept out of train/val/test and used only for the regression set, to measure generalisation to
trigger words the model has never seen.
"""

from __future__ import annotations

import random

LABELS = ["plain", "number", "date", "unit", "abbreviation", "acronym", "heteronym"]

FILLER = ("the quick team walked slowly through a quiet garden and then spoke about every small plan before lunch while another "
          "friend carried some bright paper across this long hall because nobody wanted that heavy box near our old window "
          "yesterday morning we sat by the river watching boats drift under green trees as children laughed beside warm stone "
          "walls and a gentle breeze moved over fields where farmers gathered wheat for market").split()

POOLS = {
    "number": {"train": ["12", "3.5", "1,250", "$45", "7%", "2024", "408", "0.75", "€19.99", "86", "1,000,000", "15", "9", "300",
                         "twenty three", "forty", "one hundred", "seven", "sixty five", "three thousand"],
              "heldout": ["64", "$8.25", "11,500", "2.25", "ninety", "four hundred", "5%", "17"]},
    "date": {"train": ["March 3rd", "12/05/2021", "2021-08-14", "5 June 2019", "Jan 9", "04/07/1999", "Oct 31st, 2020", "1 May 2018",
                       "2020-02-29", "Sep 12", "30/11/2015", "July 4th"],
             "heldout": ["9th of July", "2023-12-01", "Feb 14", "21/06/2017"]},
    "unit": {"train": ["5 km", "12kg", "3.5 GHz", "80 mph", "250 ml", "16 GB", "20 cm", "7 lbs", "100 mg", "512 MB", "9 oz", "45 dB",
                       "3 kilometres", "ten kilograms", "two litres"],
             "heldout": ["64 TB", "30 mm", "2.4 kHz", "6 feet", "four kilograms"]},
    "abbreviation": {"train": ["Dr. Patel", "St. Louis", "Mr. Brown", "Prof. Lee", "etc.", "approx.", "Dept. of Health", "Mrs. Clark",
                               "Ave.", "vs.", "Dr Smith", "Jr.", "Mt. Hood", "Inc.", "Sr. Garcia"],
                     "heldout": ["Capt. Reyes", "Gov. Allen", "Rev. Moore", "Ltd."]},
    "acronym": {"train": ["NASA", "HTML", "GPU", "FBI", "SQL", "USB", "NATO", "PDF", "CPU", "HTTP", "UNESCO", "RAM", "JSON", "OPEC",
                          "ASAP", "FAQ"],
                "heldout": ["LASER", "SCUBA", "DNS", "SSD", "UNICEF"]},
    "heteronym": {"train": ["read", "lead", "live", "wind", "tear", "bass", "close", "minute", "object", "record", "present", "wound",
                            "bow", "row", "refuse", "content"],
                  "heldout": ["desert", "produce", "resume", "sow", "dove", "invalid", "console", "entrance"]},
}
TEMPLATES = ["{a} {t} {b}", "{a} {t}", "{t} {b}", "{a} {t} {b} {c}", "{a} {b} {t}"]


def _filler(rng: random.Random, n: int) -> str:
    return " ".join(rng.choice(FILLER) for _ in range(n))


def make_example(rng: random.Random, label: str, pool: str = "train") -> dict:
    if label == "plain":
        text = _filler(rng, rng.randint(4, 9))
    else:
        trig = rng.choice(POOLS[label][pool])
        parts = {"a": _filler(rng, rng.randint(1, 4)), "b": _filler(rng, rng.randint(1, 4)), "c": _filler(rng, rng.randint(1, 3)),
                 "t": trig}
        text = rng.choice(TEMPLATES).format(**parts)
    text = text[0].upper() + text[1:]
    if rng.random() < 0.5:
        text += "."
    return {"text": text, "target": label}


def generate(n_per_label: int, seed: int, pool: str = "train") -> list[dict]:
    """``n_per_label`` examples for each label (so classes are balanced), from the given trigger pool."""
    rng = random.Random(seed)
    out = []
    for label in LABELS:
        for _ in range(n_per_label):
            ex = make_example(rng, label, pool)
            ex.update({"language": "en", "source": f"synthetic-{pool}", "task": "review_category"})
            out.append(ex)
    rng.shuffle(out)
    return out
