"""Decoder-only Transformer (pre-LayerNorm, learned positions, causal self-attention, GELU MLP, tied embeddings).

All weights are randomly initialised from an explicit seed. Nothing is loaded from disk here.
"""

from __future__ import annotations

import math

import torch
import torch.nn as nn
import torch.nn.functional as F

from .config import EOS, ModelConfig


class Block(nn.Module):
    def __init__(self, c: ModelConfig):
        super().__init__()
        self.ln1 = nn.LayerNorm(c.d_model)
        self.qkv = nn.Linear(c.d_model, 3 * c.attn_dim)
        self.proj = nn.Linear(c.attn_dim, c.d_model)
        self.ln2 = nn.LayerNorm(c.d_model)
        self.fc = nn.Linear(c.d_model, c.d_ff)
        self.out = nn.Linear(c.d_ff, c.d_model)
        self.n_head, self.head_dim, self.drop = c.n_head, c.head_dim, c.dropout

    def forward(self, x):
        B, T, _ = x.shape
        q, k, v = self.qkv(self.ln1(x)).view(B, T, 3, self.n_head, self.head_dim).permute(2, 0, 3, 1, 4)
        a = F.scaled_dot_product_attention(q, k, v, is_causal=True, dropout_p=self.drop if self.training else 0.0)
        x = x + self.proj(a.transpose(1, 2).reshape(B, T, -1))
        return x + self.out(F.gelu(self.fc(self.ln2(x))))


class GPT(nn.Module):
    def __init__(self, cfg: ModelConfig, seed: int | None = None):
        super().__init__()
        self.cfg = cfg
        self.init_seed = seed
        self.tok = nn.Embedding(cfg.vocab_size, cfg.d_model)
        self.pos = nn.Embedding(cfg.max_seq, cfg.d_model)
        self.blocks = nn.ModuleList(Block(cfg) for _ in range(cfg.n_layer))
        self.ln_f = nn.LayerNorm(cfg.d_model)
        self.reset_parameters(seed)

    def reset_parameters(self, seed: int | None) -> None:
        """Random initialisation. ``seed`` makes it reproducible; None draws a fresh seed (kept in init_seed)."""
        if seed is None:
            seed = int(torch.randint(0, 2**31 - 1, (1,)).item())
        self.init_seed = seed
        g = torch.Generator().manual_seed(seed)
        scale = 0.02 / math.sqrt(2 * self.cfg.n_layer)
        for name, p in self.named_parameters():
            with torch.no_grad():
                if p.dim() >= 2:
                    std = scale if name.endswith(("proj.weight", "out.weight")) else 0.02
                    p.copy_(torch.randn(p.shape, generator=g) * std)
                elif name.endswith("weight"):          # LayerNorm gain
                    p.fill_(1.0)
                else:
                    p.zero_()

    def num_params(self) -> int:
        return sum(p.numel() for p in self.parameters() if p.requires_grad)

    def forward(self, idx, targets=None, ignore_index: int = -100):
        B, T = idx.shape
        if T > self.cfg.max_seq:
            raise ValueError(f"sequence length {T} exceeds max_seq {self.cfg.max_seq}")
        x = self.tok(idx) + self.pos(torch.arange(T, device=idx.device))
        for b in self.blocks:
            x = b(x)
        logits = F.linear(self.ln_f(x), self.tok.weight)                 # tied output projection
        loss = None
        if targets is not None:
            loss = F.cross_entropy(logits.reshape(-1, logits.size(-1)), targets.reshape(-1), ignore_index=ignore_index)
        return logits, loss

    @torch.no_grad()
    def generate(self, idx, max_new_tokens: int, temperature: float = 0.0, top_k: int | None = None,
                 eos_id: int | None = EOS, generator: torch.Generator | None = None):
        """Autoregressive decoding. temperature 0 = greedy. Stops early at ``eos_id`` (batch size 1)."""
        was = self.training
        self.eval()
        for _ in range(max_new_tokens):
            logits = self(idx[:, -self.cfg.max_seq:])[0][:, -1, :]
            if temperature <= 0:
                nxt = logits.argmax(-1, keepdim=True)
            else:
                logits = logits / temperature
                if top_k:
                    v, _ = torch.topk(logits, min(top_k, logits.size(-1)))
                    logits = logits.masked_fill(logits < v[:, [-1]], float("-inf"))
                nxt = torch.multinomial(F.softmax(logits, -1), 1, generator=generator)
            idx = torch.cat([idx, nxt], 1)
            if eos_id is not None and idx.size(0) == 1 and int(nxt.item()) == eos_id:
                break
        self.train(was)
        return idx


def build_model(cfg: ModelConfig, seed: int | None = None) -> GPT:
    """Scratch model with random weights. The ONLY constructor used for new models: it never reads a checkpoint."""
    return GPT(cfg, seed)
