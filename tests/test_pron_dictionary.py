"""Pronunciation dictionary, text analysis and normalisation (all real code, no mocks)."""

import json

import pytest

from speech_engine.compare import canonical_tokens, fold_equal
from speech_engine.exceptions import InvalidInputError
from speech_engine.pron_dictionary import PronDictionary
from speech_engine.text_analysis import analyze_text
from speech_engine.text_preprocessor import normalize


@pytest.fixture
def d(tmp_path):
    return PronDictionary(tmp_path / "dict.json", ipa_vocab=set("abdefhijklmnopqrstuvwxyzəɛɪʊːˈ"))


def test_whole_word_matching_never_touches_substrings(d):
    d.add(written="Ann", hint="Anne")
    text = "Ann met Annual and Joanna; Ann's car, ANN."
    apps = d.find(text, "en-us")
    assert [a.original for a in apps] == ["Ann", "Ann", "ANN"]           # not Annual / Joanna
    assert d.apply(text, apps) == "Anne met Annual and Joanna; Anne's car, Anne."


def test_case_sensitive_and_language_scope(d):
    d.add(written="US", hint="you ess", case_sensitive=True)
    d.add(written="Kind", hint="kint", language="de")
    assert [a.original for a in d.find("the US and us", "en-us")] == ["US"]
    assert d.find("Kind", "en-us") == []
    assert len(d.find("Kind", "de")) == 1


def test_contextual_matching(d):
    d.add(written="read", correction="red", context_before=["have", "had"], category="correction")
    t = "I have read it. They read daily. We had read more."
    assert [(a.original, a.spoken) for a in d.find(t, "en-us")] == [("read", "red"), ("read", "red")]
    assert d.apply(t, d.find(t, "en-us")) == "I have red it. They read daily. We had red more."


def test_longest_phrase_wins_and_no_overlap(d):
    d.add(written="New York", hint="noo york")
    d.add(written="York", hint="yawk")
    apps = d.find("New York and York", "en-us")
    assert [a.original for a in apps] == ["New York", "York"]


def test_ipa_only_entries_are_not_faked_for_backends_without_ipa(d):
    e = d.add(written="Nguyen", ipa="wɪn")
    assert d.find("Nguyen", "en-us", ipa_ok=False) == []                  # nothing silently substituted
    a = d.find("Nguyen", "en-us", ipa_ok=True)
    assert a[0].mode == "ipa" and a[0].ipa == "wɪn"
    assert [x.id for x in d.unsupported_for("Nguyen", "en-us")] == [e.id]


def test_validation_rejects_bad_entries(d):
    with pytest.raises(InvalidInputError):
        d.add(written="x")                                               # nothing to say
    with pytest.raises(InvalidInputError):
        d.add(written="x", hint="a<script>")                             # unsafe characters
    with pytest.raises(InvalidInputError):
        d.add(written="x", ipa="q̷")                                      # not in the voice vocabulary
    with pytest.raises(InvalidInputError):
        d.add(written="x", hint="y", category="bogus")
    d.add(written="Dup", hint="dupe")
    with pytest.raises(InvalidInputError):
        d.add(written="dup", hint="again")


def test_persistence_versions_and_snapshots(tmp_path):
    p = tmp_path / "d.json"
    d1 = PronDictionary(p)
    e = d1.add(written="Kubernetes", hint="koo ber net eez")
    assert d1.version == 1
    d1.update(e.id, hint="koo-ber-net-eez")
    assert d1.version == 2
    d2 = PronDictionary(p)
    assert d2.version == 2 and d2.entries[e.id].hint == "koo-ber-net-eez"
    assert (p.parent / "history" / "dictionary-v00001.json").is_file()
    d2.delete(e.id)
    assert PronDictionary(p).entries == {}


def test_search_import_export_roundtrip(d):
    d.add(written="Nguyen", hint="win", category="name")
    d.add(written="GIF", hint="jiff", category="acronym")
    assert [e.written for e in d.search("ng")] == ["Nguyen"]
    assert [e.written for e in d.search(category="acronym")] == ["GIF"]
    for fmt in ("json", "csv"):
        text = d.export(fmt)
        d2 = PronDictionary(memory=True)
        res = d2.import_text(text, fmt)
        assert res["added"] == 2
        assert {e.written for e in d2.entries.values()} == {"Nguyen", "GIF"}
    assert d.import_text(d.export("json"), "json")["skipped"] == 2        # existing entries are not duplicated


def test_import_is_all_or_nothing(d):
    bad = json.dumps([{"written": "Good", "hint": "gud"}, {"written": "Bad"}])
    with pytest.raises(InvalidInputError):
        d.import_text(bad, "json")
    assert d.entries == {}


# --------------------------------------------------------------------------- normalisation / analysis
def test_normalisation_keeps_meaning_and_original_offsets():
    assert normalize("Dr. Smith met exam-\nple.", True) == "Doctor Smith met example."
    t = "Ünïcode café — naïve: 你好 мир"
    assert normalize(t) == t                                              # multilingual text untouched


@pytest.mark.parametrize("a,b", [
    ("In 2024, he paid $5.50.", "in twenty twenty four he paid five dollars and fifty cents"),
    ("It's 50% off", "it is fifty percent off"), ("The 21st", "the twenty first"),
    ("1,000 people", "one thousand people"), ("pi is 3.14", "pi is three point one four"),
    ("Hello, world!", "hello world"), ("Call 3:30", "call three thirty"), ("don't", "do not"),
])
def test_harmless_differences_are_equal(a, b):
    assert fold_equal(a, b)


@pytest.mark.parametrize("a,b", [("In 2024", "in 2025"), ("five cats", "six cats"), ("read it", "red it"), ("Nguyen", "win")])
def test_real_differences_are_not_equal(a, b):
    assert not fold_equal(a, b)


def test_multilingual_tokens_survive():
    toks = [t.norm for t in canonical_tokens("Привет мир 你好")]
    assert toks[:2] == ["привет", "мир"]


def test_analysis_flags_without_rewriting(d):
    d.add(written="Nguyen", hint="win", category="name")
    text = "Dr. Nguyen read the API docs on 03/04/2025. Привет! Visit kubectl_v2 at 5 km."
    r = analyze_text(text, "en-us", d)
    kinds = {f.kind for f in r.findings}
    assert {"dictionary", "heteronym", "acronym", "date", "multilingual", "technical", "unit", "abbreviation"} <= kinds
    ambiguous_date = next(f for f in r.findings if f.kind == "date")
    assert ambiguous_date.severity == "review"
    assert text[ambiguous_date.start:ambiguous_date.end] == "03/04/2025"  # offsets point into the ORIGINAL text
    assert r.applications[0]["original"] == "Nguyen"
    assert any("English-only" in n for n in analyze_text("Hola mundo", "es").notes)
