"""A tiny trainable tokenizer: characters, plus optional learned BPE merges. Learned from the given data only (nothing pretrained)."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from .config import BOS, EOS, SPECIALS, UNK


class Tokenizer:
    def __init__(self, vocab: list[str], merges: list[tuple[str, str]] | None = None):
        if list(vocab[: len(SPECIALS)]) != list(SPECIALS):
            raise ValueError("vocab must start with the special tokens")
        if len(set(vocab)) != len(vocab):
            raise ValueError("duplicate tokens in vocab")
        self.vocab = list(vocab)
        self.merges = [tuple(m) for m in (merges or [])]
        self.index = {t: i for i, t in enumerate(self.vocab)}
        self._rank = {m: i for i, m in enumerate(self.merges)}

    @property
    def size(self) -> int:
        return len(self.vocab)

    @classmethod
    def from_alphabet(cls, chars) -> "Tokenizer":
        """Character tokenizer over a FIXED alphabet (the vocabulary must not change once a model exists)."""
        return cls(list(SPECIALS) + sorted(set(chars)))

    @classmethod
    def train(cls, texts, vocab_size: int | None = None) -> "Tokenizer":
        """Characters seen in ``texts``; if ``vocab_size`` is larger, add BPE merges until it is reached (or no pair repeats)."""
        words: Counter = Counter()
        chars: set[str] = set()
        for t in texts:
            chars.update(t)
            for w in t.split(" "):
                words[tuple(w) + (" ",)] += 1
        vocab = list(SPECIALS) + sorted(chars)
        merges: list[tuple[str, str]] = []
        limit = vocab_size or len(vocab)
        if limit < len(vocab):
            raise ValueError(f"vocab_size {limit} is smaller than the {len(vocab)} distinct characters in the data")
        while len(vocab) < limit:
            pairs: Counter = Counter()
            for w, n in words.items():
                for a, b in zip(w, w[1:]):
                    pairs[(a, b)] += n
            if not pairs:
                break
            (a, b), n = max(pairs.items(), key=lambda kv: (kv[1], kv[0]))
            if n < 2:
                break
            merges.append((a, b))
            vocab.append(a + b)
            new: Counter = Counter()
            for w, c in words.items():
                out, i = [], 0
                while i < len(w):
                    if i + 1 < len(w) and w[i] == a and w[i + 1] == b:
                        out.append(a + b)
                        i += 2
                    else:
                        out.append(w[i])
                        i += 1
                new[tuple(out)] += c
            words = new
        return cls(vocab, merges)

    def _bpe(self, word: tuple[str, ...]) -> list[str]:
        w = list(word)
        while len(w) > 1:
            r, i = min((self._rank.get((a, b), 1 << 30), i) for i, (a, b) in enumerate(zip(w, w[1:])))
            if r == 1 << 30:
                break
            w[i:i + 2] = [w[i] + w[i + 1]]
        return w

    def encode(self, text: str, bos: bool = False, eos: bool = False) -> list[int]:
        ids = [BOS] if bos else []
        if self.merges:
            parts = text.split(" ")
            for k, w in enumerate(parts):
                toks = self._bpe(tuple(w) + ((" ",) if k < len(parts) - 1 else ()))
                ids.extend(self.index.get(t, UNK) for t in toks)
        else:
            ids.extend(self.index.get(c, UNK) for c in text)
        if eos:
            ids.append(EOS)
        return ids

    def decode(self, ids) -> str:
        return "".join(self.vocab[i] for i in ids if len(SPECIALS) <= i < len(self.vocab))

    def to_dict(self) -> dict:
        return {"vocab": self.vocab, "merges": [list(m) for m in self.merges]}

    def save(self, path: Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), ensure_ascii=False), encoding="utf-8")

    @classmethod
    def from_dict(cls, d: dict) -> "Tokenizer":
        return cls(d["vocab"], [tuple(m) for m in d.get("merges", [])])

    @classmethod
    def load(cls, path: Path) -> "Tokenizer":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
