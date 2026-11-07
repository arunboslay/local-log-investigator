"""Two small anomaly models that learn from normal traffic only. Both score one request on the CPU.

- Isolation Forest: looks at numbers about a request (endpoint, status, speed, SQL count, how many
  lines and warnings). Odd combinations are easy to isolate, so they score as anomalies.
- Sequence model (DeepLog style): a tiny LSTM that predicts each next log message from the ones before.
  A request is flagged when the real next message isn't among its top guesses, or it ends early.

Each is given a flag threshold from the training traffic only, never from fault recordings.
"""

import math
import pickle
import time
from collections import Counter
from pathlib import Path

import numpy as np
import torch
from sklearn.ensemble import IsolationForest
from torch import nn

from watcher.request_data import RequestRecord

END, UNK = "<end>", "<unk>"


class ForestModel:
    def __init__(self, seed: int = 7):
        self.forest = IsolationForest(n_estimators=200, random_state=seed)
        self.endpoints: list[str] = []
        self.known: set[str] = set()
        self.threshold = 0.0

    def _features(self, recs: list[RequestRecord]) -> np.ndarray:
        rows = []
        for r in recs:
            one_hot = [1.0 if r.endpoint == e else 0.0 for e in self.endpoints]
            rows.append(one_hot + [r.status // 100, math.log1p(r.latency_ms), r.db_queries, len(r.templates),
                                   r.warns, r.errors, sum(t not in self.known for t in r.templates)])
        return np.array(rows, dtype=float)

    def fit(self, recs: list[RequestRecord], quantile: float = 0.001):
        self.endpoints = sorted({r.endpoint for r in recs})
        self.known = {t for r in recs for t in r.templates}
        X = self._features(recs)
        self.forest.fit(X)
        # Lower scores are stranger. Flag about the strangest 0.1% of normal traffic and below.
        self.threshold = float(np.quantile(self.forest.score_samples(X), quantile))
        return self

    def flags(self, recs: list[RequestRecord]) -> list[bool]:
        return list(self.forest.score_samples(self._features(recs)) < self.threshold) if recs else []


class _Net(nn.Module):
    def __init__(self, vocab: int, embed: int = 32, hidden: int = 64):
        super().__init__()
        self.embed = nn.Embedding(vocab, embed)
        self.lstm = nn.LSTM(embed, hidden, batch_first=True)
        self.out = nn.Linear(hidden, vocab)

    def forward(self, x):
        h, _ = self.lstm(self.embed(x))
        return self.out(h)


class SequenceModel:
    def __init__(self, top_k: int = 2):
        self.top_k = top_k
        self.vocab: dict[str, int] = {}
        self.net: _Net | None = None
        self.info: dict = {}

    def _tokens(self, r: RequestRecord) -> list[int]:
        unk = self.vocab[UNK]
        return [self.vocab.get("EP " + r.endpoint, unk)] + [self.vocab.get(t, unk) for t in r.templates] + [self.vocab[END]]

    def fit(self, recs: list[RequestRecord], epochs: int = 4, device: str | None = None, seed: int = 7):
        torch.manual_seed(seed)
        device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        names = [UNK, END] + sorted({"EP " + r.endpoint for r in recs}) + sorted({t for r in recs for t in r.templates})
        self.vocab = {name: i for i, name in enumerate(names)}
        seqs = [self._tokens(r) for r in recs]
        net = _Net(len(names)).to(device)
        optimizer = torch.optim.Adam(net.parameters(), lr=3e-3)
        loss_fn = nn.CrossEntropyLoss()
        start = time.perf_counter()
        order = np.random.default_rng(seed)
        losses = []
        for _ in range(epochs):
            epoch_loss = 0.0
            batches = 0
            idx = order.permutation(len(seqs))
            # Requests with the same length batch together without padding.
            by_len: dict[int, list[int]] = {}
            for i in idx:
                by_len.setdefault(len(seqs[i]), []).append(i)
            for length, members in by_len.items():
                for b in range(0, len(members), 256):
                    batch = torch.tensor([seqs[i] for i in members[b:b + 256]], device=device)
                    logits = net(batch[:, :-1])
                    loss = loss_fn(logits.reshape(-1, len(names)), batch[:, 1:].reshape(-1))
                    optimizer.zero_grad()
                    loss.backward()
                    optimizer.step()
                    epoch_loss += loss.item()
                    batches += 1
            losses.append(round(epoch_loss / max(batches, 1), 4))
        self.net = net.cpu().eval()
        self.info = {"params": sum(p.numel() for p in net.parameters()), "vocab": len(names), "train_requests": len(recs),
                     "epochs": epochs, "train_seconds": round(time.perf_counter() - start, 1), "device": device, "losses": losses}
        return self

    @torch.no_grad()
    def explain(self, r: RequestRecord) -> str | None:
        """None if the request looks normal, else why it was flagged."""
        tokens = self._tokens(r)
        names = list(self.vocab)
        if self.vocab[UNK] in tokens:
            return "a log message never seen in normal traffic: " + next(
                (t for t in r.templates if t not in self.vocab), r.endpoint)
        logits = self.net(torch.tensor([tokens[:-1]]))[0]
        top = logits.topk(min(self.top_k, logits.shape[-1]), dim=-1).indices
        for i, actual in enumerate(tokens[1:]):
            if actual not in top[i].tolist():
                what = "ended early" if actual == self.vocab[END] else f"unexpected next message: {names[actual]}"
                return f"{what} (expected: {names[top[i][0].item()]})"
        return None

    def flag(self, r: RequestRecord) -> bool:
        return self.explain(r) is not None

    def train_flag_rate(self, recs: list[RequestRecord]) -> float:
        return sum(map(self.flag, recs)) / max(len(recs), 1)


def choose_top_k(model: SequenceModel, recs: list[RequestRecord], max_rate: float = 0.001) -> int:
    """The smallest top-k that keeps flags on normal training traffic below max_rate."""
    unique = Counter((r.endpoint, tuple(r.templates)) for r in recs)
    sample = {}
    for r in recs:
        sample.setdefault((r.endpoint, tuple(r.templates)), r)
    for k in (1, 2, 3, 5, 8):
        model.top_k = k
        flagged = sum(n for key, n in unique.items() if model.flag(sample[key]))
        if flagged / len(recs) <= max_rate:
            return k
    return model.top_k


def save(obj, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        pickle.dump(obj, f)


def load(path: Path):
    with open(path, "rb") as f:
        return pickle.load(f)
