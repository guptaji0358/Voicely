"""Memory inspection, pre-allocation estimates and budget decisions. Training never allocates tensors before this has said yes."""

from __future__ import annotations

import ctypes
import os
import platform
from dataclasses import asdict, dataclass

from .config import ModelConfig

GB = 1024**3
RUNTIME_OVERHEAD = int(0.30 * GB)        # interpreter + torch + allocator slack


def system_memory() -> dict:
    """{'total','available'} in bytes (Windows: GlobalMemoryStatusEx, Linux: /proc/meminfo)."""
    if platform.system() == "Windows":
        class MS(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong), ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong), ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong), ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong), ("sullAvailExtendedVirtual", ctypes.c_ulonglong)]
        s = MS()
        s.dwLength = ctypes.sizeof(MS)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(s))
        return {"total": int(s.ullTotalPhys), "available": int(s.ullAvailPhys)}
    try:
        info = {k: int(v.split()[0]) * 1024 for k, v in (l.split(":", 1) for l in open("/proc/meminfo"))}
        return {"total": info["MemTotal"], "available": info["MemAvailable"]}
    except OSError:
        return {"total": 0, "available": 0}


def _win_counters():
    """(WorkingSetSize, PeakWorkingSetSize) of this process via kernel32 with correct 64-bit argument types."""
    from ctypes import wintypes

    class PMC(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD), ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t), ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t)]
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.GetCurrentProcess.restype = wintypes.HANDLE
    k.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
    k.K32GetProcessMemoryInfo.restype = wintypes.BOOL
    c = PMC()
    c.cb = ctypes.sizeof(PMC)
    if not k.K32GetProcessMemoryInfo(k.GetCurrentProcess(), ctypes.byref(c), c.cb):
        return 0, 0
    return int(c.WorkingSetSize), int(c.PeakWorkingSetSize)


def peak_rss() -> int:
    """Peak resident set size of THIS process in bytes (0 when unknown)."""
    if platform.system() == "Windows":
        return _win_counters()[1]
    try:
        import resource
        return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * 1024
    except Exception:
        return 0


def current_rss() -> int:
    if platform.system() == "Windows":
        return _win_counters()[0]
    return peak_rss()


def hardware_report() -> dict:
    import torch
    m = system_memory()
    return {"cpu_count": os.cpu_count(), "ram_total": m["total"], "ram_available": m["available"],
            "torch": torch.__version__, "cuda": bool(torch.cuda.is_available()),
            "cuda_device": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
            "torch_threads": torch.get_num_threads()}


def estimate_training_bytes(cfg: ModelConfig, batch_size: int, seq_len: int | None = None, optimizer: str = "adamw") -> dict:
    """Upper-ish estimate of peak training memory in bytes, computed from the config alone (nothing is allocated).

    weights + gradients (fp32) + optimizer moments, plus activations kept for backward (rough per-layer formula for a
    pre-LN Transformer in fp32) and the logits. It is an ESTIMATE; the run also reports the measured peak.
    """
    T = min(seq_len or cfg.max_seq, cfg.max_seq)
    P = cfg.param_count()
    weights, grads = 4 * P, 4 * P
    opt = {"adamw": 8 * P, "sgd": 4 * P}.get(optimizer, 8 * P)
    d, f, a, L, B = cfg.d_model, cfg.d_ff, cfg.attn_dim, cfg.n_layer, batch_size
    per_layer = B * T * (4 * d + 4 * a + 2 * f + 3 * d) * 4 + B * cfg.n_head * T * T * 4 * 2
    acts = L * per_layer + B * T * d * 4 * 3
    logits = B * T * cfg.vocab_size * 4 * 3
    total = weights + grads + opt + acts + logits + RUNTIME_OVERHEAD
    return {"parameters": P, "weights": weights, "gradients": grads, "optimizer": opt, "activations": acts,
            "logits": logits, "overhead": RUNTIME_OVERHEAD, "total": total}


def _env_gb(name: str, default: float) -> int:
    try:
        return int(float(os.environ.get(name, default)) * GB)
    except ValueError:
        return int(default * GB)


@dataclass
class Budget:
    # left free for Windows, Qt, the speech engine and other applications (override: MODEL_LAB_SAFETY_MARGIN_GB)
    safety_margin: int = _env_gb("MODEL_LAB_SAFETY_MARGIN_GB", 1.0)
    hard_floor: int = _env_gb("MODEL_LAB_HARD_FLOOR_GB", 0.4)    # never go below this, even when the user confirms


@dataclass
class Decision:
    verdict: str                              # "ok" | "needs_confirmation" | "refuse"
    reason: str
    estimated: int
    available: int
    allowed: int

    def to_dict(self) -> dict:
        return asdict(self)


def check_budget(estimated: int, available: int | None = None, budget: Budget | None = None, confirmed: bool = False) -> Decision:
    """ok: fits with the safety margin. needs_confirmation: fits only by eating into the margin. refuse: would leave < hard floor."""
    b = budget or Budget()
    avail = system_memory()["available"] if available is None else available
    allowed = max(0, avail - b.safety_margin)
    gb = lambda x: f"{x / GB:.2f} GB"
    if estimated <= allowed:
        return Decision("ok", f"needs ~{gb(estimated)}; {gb(avail)} free, {gb(allowed)} usable after the {gb(b.safety_margin)} safety margin",
                        estimated, avail, allowed)
    if estimated <= avail - b.hard_floor:
        if confirmed:
            return Decision("ok", f"confirmed: ~{gb(estimated)} exceeds the {gb(allowed)} usable memory but leaves the {gb(b.hard_floor)} floor",
                            estimated, avail, allowed)
        return Decision("needs_confirmation",
                        f"needs ~{gb(estimated)} but only {gb(allowed)} is usable within the safety margin ({gb(avail)} free). "
                        "Close other applications or confirm to proceed.", estimated, avail, allowed)
    return Decision("refuse", f"needs ~{gb(estimated)} but only {gb(avail)} is free; refusing to start (floor {gb(b.hard_floor)})",
                    estimated, avail, allowed)
