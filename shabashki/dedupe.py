"""Отсев повторов: одну и ту же шабашку часто кидают в 3–5 чатов подряд."""
from __future__ import annotations

import hashlib
import re
import time

from .extract import norm


def _words(text: str) -> list[str]:
    t = norm(text)
    t = re.sub(r"https?://\S+|t\.me/\S+|@\w+|[^\w\s]", " ", t)
    return t.split()


def simhash(text: str) -> int:
    words = _words(text)
    if len(words) < 3:
        shingles = words or [""]
    else:
        shingles = [" ".join(words[i:i + 3]) for i in range(len(words) - 2)]
    acc = [0] * 64
    for s in shingles:
        h = int.from_bytes(hashlib.blake2b(s.encode("utf-8"), digest_size=8).digest(), "big")
        for i in range(64):
            acc[i] += 1 if (h >> i) & 1 else -1
    out = 0
    for i in range(64):
        if acc[i] > 0:
            out |= 1 << i
    return out


def _dist(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


class Seen:
    """Отпечатки последних постов (хранятся в состоянии как hex-строки)."""

    def __init__(self, items: list | None = None, days: int = 4, cap: int = 3000):
        cutoff = time.time() - days * 86400
        self.cap = cap
        self.items: list[tuple[int, int, int]] = []   # (hash, ts, n_words)
        for it in items or []:
            try:
                h, ts, n = int(it[0], 16), int(it[1]), int(it[2])
            except (ValueError, TypeError, IndexError):
                continue
            if ts >= cutoff:
                self.items.append((h, ts, n))

    def is_dup(self, text: str) -> bool:
        h, n = simhash(text), len(_words(text))
        limit = 5 if n >= 8 else 0
        return any(_dist(h, x) <= limit for x, _, m in self.items if (m >= 8) == (n >= 8))

    def add(self, text: str, ts: int | None = None) -> None:
        self.items.append((simhash(text), int(ts or time.time()), len(_words(text))))
        if len(self.items) > self.cap:
            self.items = self.items[-self.cap:]

    def dump(self) -> list:
        return [[f"{h:016x}", ts, n] for h, ts, n in self.items]
