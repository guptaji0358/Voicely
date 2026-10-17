"""Persistent, versioned pronunciation dictionary.

An entry says how one written form (word or short phrase) must be spoken:
  * ``hint``       respelling the TTS reads instead of the written form ("Nguyen" -> "win")
  * ``ipa``        IPA used where the backend can take phonemes (local Kokoro); validated against its vocabulary
  * ``correction`` a user-approved replacement phrase (e.g. "Dr." -> "Doctor"); wins over ``hint``
Matching is whole-word / whole-phrase and Unicode aware ("Ann" never matches inside "Annual"), optionally
restricted by neighbouring words (``context_before`` / ``context_after``), e.g. "read" after "have".
Nothing is ever replaced by guesswork: only entries a person created or approved are applied, and every
application is recorded so audio can be traced back to the source text.

Every mutation bumps ``version`` and keeps a snapshot, so a change can be evaluated against the regression set
(regression.py) and rolled back.
"""

from __future__ import annotations

import csv
import io
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from .exceptions import InvalidInputError
from .paths import data_dir, read_json, write_json_atomic

SCHEMA = 1
CATEGORIES = ("name", "technical", "acronym", "abbreviation", "custom", "correction")
SOURCES = ("user", "feedback", "llm-approved", "import")
MAX_FIELD = 200
_IPA_EXTRA = set(" ˈˌː.-")           # stress, length, syllable marks that are always allowed with IPA letters
_HINT_OK = re.compile(r"^[\w .,'’\-]{1,%d}$" % MAX_FIELD, re.UNICODE)
_CTRL = re.compile(r"[\x00-\x1f\x7f]")


@dataclass
class Entry:
    written: str
    language: str = "*"              # "*" = any language, or a code such as "en-us" ("en" also matches en-gb)
    hint: str = ""
    ipa: str = ""
    correction: str = ""
    category: str = "custom"
    case_sensitive: bool = False
    context_before: list[str] = field(default_factory=list)   # one of these words must directly precede
    context_after: list[str] = field(default_factory=list)    # one of these words must directly follow
    note: str = ""
    enabled: bool = True
    source: str = "user"
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)

    def spoken_text(self) -> str:
        return self.correction or self.hint

    def validate(self, ipa_vocab: set[str] | None = None) -> None:
        def clean(name: str, v: str, maxlen=MAX_FIELD):
            if not isinstance(v, str) or _CTRL.search(v) or len(v) > maxlen:
                raise InvalidInputError(f"{name} must be text of at most {maxlen} characters without control characters")
        clean("written", self.written)
        if not self.written.strip():
            raise InvalidInputError("written form is required")
        for n in ("hint", "correction", "note", "language"):
            clean(n, getattr(self, n))
        for n in ("hint", "correction"):
            v = getattr(self, n)
            if v and not _HINT_OK.match(v):
                raise InvalidInputError(f"{n} may only contain letters, digits, spaces and . , ' -")
        if self.category not in CATEGORIES:
            raise InvalidInputError(f"category must be one of {', '.join(CATEGORIES)}")
        if self.source not in SOURCES:
            raise InvalidInputError(f"source must be one of {', '.join(SOURCES)}")
        if not (self.hint or self.ipa or self.correction):
            raise InvalidInputError("an entry needs a hint, an IPA transcription or a correction")
        if self.ipa:
            clean("ipa", self.ipa, 120)
            if ipa_vocab is not None:
                bad = sorted({c for c in self.ipa if c not in ipa_vocab and c not in _IPA_EXTRA and not c.isspace()})
                if bad:
                    raise InvalidInputError("IPA contains symbols the local voice cannot pronounce: " + " ".join(bad))
        for n in ("context_before", "context_after"):
            lst = getattr(self, n)
            if not isinstance(lst, list) or len(lst) > 20 or any(not isinstance(w, str) or not w or len(w) > 40 for w in lst):
                raise InvalidInputError(f"{n} must be a short list of words")

    def lang_ok(self, lang: str | None) -> bool:
        if self.language in ("*", "", None) or not lang:
            return True
        a, b = self.language.lower(), lang.lower()
        return a == b or a.split("-")[0] == b.split("-")[0]


@dataclass
class Application:
    """One dictionary entry applied to one place in a text (offsets are into the text it was applied to)."""
    start: int
    end: int
    original: str
    entry_id: str
    spoken: str            # text actually sent to the TTS for this span
    mode: str              # "text" (respelling/correction substituted) | "ipa" (phonemes given to the backend)
    ipa: str = ""


def _compile(e: Entry) -> re.Pattern:
    words = re.split(r"\s+", e.written.strip())
    body = r"\s+".join(re.escape(w) for w in words)
    return re.compile(r"(?<![\w])" + body + r"(?![\w])", 0 if e.case_sensitive else re.IGNORECASE | re.UNICODE)


def _neighbour_ok(text: str, start: int, end: int, before: list[str], after: list[str]) -> bool:
    if before:
        m = re.search(r"([\w'’\-]+)\s*$", text[max(0, start - 60):start], re.UNICODE)
        if not m or m.group(1).lower() not in {w.lower() for w in before}:
            return False
    if after:
        m = re.match(r"\s*([\w'’\-]+)", text[end:end + 60], re.UNICODE)
        if not m or m.group(1).lower() not in {w.lower() for w in after}:
            return False
    return True


class PronDictionary:
    def __init__(self, path: Path | None = None, ipa_vocab: set[str] | None = None, memory: bool = False):
        self.memory = memory                      # in-memory copy (regression checks); never touches disk
        self.path = None if memory else (Path(path) if path else data_dir() / "pronunciation" / "dictionary.json")
        self.ipa_vocab = ipa_vocab
        self._lock = threading.RLock()
        self.version = 0
        self.entries: dict[str, Entry] = {}
        self._compiled: dict[str, re.Pattern] = {}
        self._load()

    # ---- persistence -------------------------------------------------------
    @classmethod
    def in_memory(cls, entries: list["Entry"]) -> "PronDictionary":
        d = cls(memory=True)
        d.entries = {e.id: e for e in entries}
        return d

    def _load(self) -> None:
        if self.memory:
            return
        data = read_json(self.path, None)
        if not data:
            return
        if data.get("schema") != SCHEMA:
            raise InvalidInputError(f"unsupported dictionary schema {data.get('schema')!r}")
        known = {f.name for f in fields(Entry)}
        self.version = int(data.get("version", 0))
        for raw in data.get("entries", []):
            e = Entry(**{k: v for k, v in raw.items() if k in known})
            self.entries[e.id] = e

    def _save(self) -> None:
        self.version += 1
        if self.memory:
            return
        doc = {"schema": SCHEMA, "version": self.version, "updated": time.time(),
               "entries": [asdict(e) for e in self.entries.values()]}
        write_json_atomic(self.path, doc)
        hist = self.path.parent / "history"
        write_json_atomic(hist / f"dictionary-v{self.version:05d}.json", doc)
        for old in sorted(hist.glob("dictionary-v*.json"))[:-30]:   # keep the last 30 snapshots
            old.unlink(missing_ok=True)

    # ---- CRUD ----------------------------------------------------------------
    def _check_duplicate(self, e: Entry) -> None:
        for o in self.entries.values():
            if o.id != e.id and o.written.lower() == e.written.lower() and o.language == e.language \
                    and o.context_before == e.context_before and o.context_after == e.context_after:
                raise InvalidInputError(f"an entry for {e.written!r} ({e.language}) with the same context already exists")

    def add(self, **kw) -> Entry:
        with self._lock:
            e = self._build(kw)
            e.validate(self.ipa_vocab)
            self._check_duplicate(e)
            self.entries[e.id] = e
            self._save()
            return e

    def update(self, entry_id: str, **kw) -> Entry:
        with self._lock:
            old = self.entries.get(entry_id)
            if not old:
                raise InvalidInputError(f"no dictionary entry {entry_id!r}")
            merged = {**asdict(old), **{k: v for k, v in kw.items() if k not in ("id", "created")}}
            e = self._build(merged)
            e.created, e.updated = old.created, time.time()
            e.validate(self.ipa_vocab)
            self._check_duplicate(e)
            self.entries[e.id] = e
            self._compiled.pop(e.id, None)
            self._save()
            return e

    def delete(self, entry_id: str) -> None:
        with self._lock:
            if entry_id not in self.entries:
                raise InvalidInputError(f"no dictionary entry {entry_id!r}")
            del self.entries[entry_id]
            self._compiled.pop(entry_id, None)
            self._save()

    @staticmethod
    def _build(d: dict) -> Entry:
        known = {f.name for f in fields(Entry)}
        unknown = set(d) - known
        if unknown:
            raise InvalidInputError(f"unknown entry fields: {sorted(unknown)}")
        for k in ("context_before", "context_after"):
            v = d.get(k)
            if isinstance(v, str):
                d[k] = [w for w in re.split(r"[\s,]+", v) if w]
        return Entry(**d)

    def search(self, query: str = "", category: str | None = None) -> list[Entry]:
        q = (query or "").strip().lower()
        out = [e for e in self.entries.values()
               if (not category or e.category == category)
               and (not q or q in e.written.lower() or q in e.hint.lower() or q in e.correction.lower() or q in e.note.lower())]
        return sorted(out, key=lambda e: (e.written.lower(), e.language))

    # ---- import / export -----------------------------------------------------------
    CSV_COLS = ["written", "language", "hint", "ipa", "correction", "category", "case_sensitive",
                "context_before", "context_after", "note"]

    def export(self, fmt: str = "json") -> str:
        if fmt == "json":
            import json
            return json.dumps({"schema": SCHEMA, "version": self.version, "entries": [asdict(e) for e in self.search()]},
                              ensure_ascii=False, indent=2)
        if fmt == "csv":
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(self.CSV_COLS)
            for e in self.search():
                w.writerow([e.written, e.language, e.hint, e.ipa, e.correction, e.category, int(e.case_sensitive),
                            " ".join(e.context_before), " ".join(e.context_after), e.note])
            return buf.getvalue()
        raise InvalidInputError("format must be json or csv")

    def import_text(self, text: str, fmt: str, replace_existing: bool = False) -> dict:
        """All-or-nothing: every row is validated first; nothing is stored if any row is invalid."""
        rows: list[dict] = []
        if fmt == "json":
            import json
            try:
                doc = json.loads(text)
            except ValueError as exc:
                raise InvalidInputError(f"invalid JSON: {exc}") from exc
            items = doc.get("entries") if isinstance(doc, dict) else doc
            if not isinstance(items, list):
                raise InvalidInputError("JSON must be a list of entries or {entries: [...]}")
            for it in items:
                if not isinstance(it, dict):
                    raise InvalidInputError("each entry must be an object")
                rows.append({k: v for k, v in it.items() if k not in ("id", "created", "updated")})
        elif fmt == "csv":
            rd = csv.DictReader(io.StringIO(text))
            if not rd.fieldnames or "written" not in rd.fieldnames:
                raise InvalidInputError("CSV needs a header row with a 'written' column")
            for r in rd:
                row = {k: (v or "") for k, v in r.items() if k in self.CSV_COLS}
                row["case_sensitive"] = str(row.get("case_sensitive", "")).strip().lower() in ("1", "true", "yes")
                row["language"] = row.get("language") or "*"
                row["category"] = row.get("category") or "custom"
                rows.append(row)
        else:
            raise InvalidInputError("format must be json or csv")
        if len(rows) > 5000:
            raise InvalidInputError("too many entries (max 5000 per import)")
        with self._lock:
            staged = dict(self.entries)
            added = updated = skipped = 0
            for r in rows:
                r = dict(r)
                r["source"] = "import"
                e = self._build(r)
                e.validate(self.ipa_vocab)
                dup = next((o for o in staged.values() if o.written.lower() == e.written.lower() and o.language == e.language
                            and o.context_before == e.context_before and o.context_after == e.context_after), None)
                if dup and not replace_existing:
                    skipped += 1
                elif dup:
                    e.id, e.created = dup.id, dup.created
                    staged[e.id] = e
                    updated += 1
                else:
                    staged[e.id] = e
                    added += 1
            self.entries = staged
            self._compiled.clear()
            if added or updated:
                self._save()
        return {"added": added, "updated": updated, "skipped": skipped, "version": self.version}

    # ---- matching ---------------------------------------------------------------------
    def find(self, text: str, language: str | None = None, ipa_ok: bool = False) -> list[Application]:
        """Non-overlapping applications, longest/most specific first. Entries whose only content is IPA are
        applied only when ``ipa_ok`` (backend can take phonemes); otherwise they are skipped, never faked."""
        cands: list[tuple[int, int, int, Entry]] = []
        with self._lock:
            entries = [e for e in self.entries.values() if e.enabled and e.lang_ok(language)]
            for e in entries:
                if e.id not in self._compiled:
                    self._compiled[e.id] = _compile(e)
                for m in self._compiled[e.id].finditer(text):
                    if not _neighbour_ok(text, m.start(), m.end(), e.context_before, e.context_after):
                        continue
                    specificity = (1 if e.context_before or e.context_after else 0) + (1 if e.case_sensitive else 0)
                    cands.append((m.start(), m.end(), specificity, e))
        cands.sort(key=lambda c: (c[0], -(c[1] - c[0]), -c[2]))
        apps: list[Application] = []
        last_end = -1
        for s, en, _spec, e in cands:
            if s < last_end:
                continue
            spoken = e.spoken_text()
            if spoken:
                apps.append(Application(s, en, text[s:en], e.id, spoken, "text", e.ipa))
            elif e.ipa and ipa_ok:
                apps.append(Application(s, en, text[s:en], e.id, text[s:en], "ipa", e.ipa))
            else:
                continue          # IPA-only entry but backend cannot use IPA: report elsewhere, do nothing here
            last_end = en
        return apps

    def unsupported_for(self, text: str, language: str | None) -> list[Entry]:
        """IPA-only entries present in ``text`` that the active backend cannot honour."""
        found = {a.entry_id for a in self.find(text, language, ipa_ok=True) if a.mode == "ipa"}
        return [self.entries[i] for i in found]

    @staticmethod
    def apply(text: str, apps: list[Application]) -> str:
        out, pos = [], 0
        for a in sorted(apps, key=lambda a: a.start):
            if a.mode != "text":
                continue
            out.append(text[pos:a.start])
            out.append(a.spoken)
            pos = a.end
        out.append(text[pos:])
        return "".join(out)
