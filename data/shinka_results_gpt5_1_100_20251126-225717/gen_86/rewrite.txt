# EVOLVE-BLOCK-START
"""W-TinyLFU + SLRU:
- Window LRU (W) for new items
- Main SLRU split into Probation (P) and Protected (S)
- TinyLFU-like decayed frequency for admission
- Adaptive window sizing and scan-aware eviction
"""

from collections import OrderedDict

# Segments
_W_window = OrderedDict()   # recency-biased window
_P_prob = OrderedDict()     # main probation
_S_prot = OrderedDict()     # main protected
_seg = {}                   # key -> 'W'|'P'|'S'

# Decayed frequency (TinyLFU-like) and timestamps
_freq = {}                  # key -> small int, halved periodically
_FREQ_MAX = 15
_last_age_tick = 0
_AGE_WINDOW = 128

# EWMA signals for scan/locality
_hit_ewma = 0.0
_miss_ewma = 0.0
_EWMA_ALPHA = 0.05
_scan_mode = False

# Capacity and targets
_cap_est = 0
_window_frac = 0.10    # adaptive: 2%..25%
_prot_frac = 0.80      # protected share within main (P+S)

# Admission planning between evict() and update_after_insert()
_admit_plan = {}        # key -> 'main' | 'window'


def _ensure_params(cache_snapshot):
    global _cap_est, _AGE_WINDOW
    cap = getattr(cache_snapshot, "capacity", None)
    if isinstance(cap, int) and cap > 0:
        _cap_est = cap
    else:
        _cap_est = max(_cap_est, len(cache_snapshot.cache))
    _cap_est = max(1, _cap_est)
    # Age roughly at cache size cadence
    _AGE_WINDOW = max(64, _cap_est)


def _maybe_age(cache_snapshot):
    """Halve frequencies periodically to discard stale history."""
    global _last_age_tick
    now = cache_snapshot.access_count
    if now - _last_age_tick >= _AGE_WINDOW:
        for k in list(_freq.keys()):
            nv = _freq.get(k, 0) // 2
            if nv <= 0:
                _freq.pop(k, None)
            else:
                _freq[k] = nv
        _last_age_tick = now


def _inc_freq(key):
    _freq[key] = min(_FREQ_MAX, _freq.get(key, 0) + 1)


def _freq_of(key):
    return _freq.get(key, 0)


def _update_signals(is_hit, cache_snapshot):
    """Update EWMAs and detect scans to adapt window sizing."""
    global _hit_ewma, _miss_ewma, _scan_mode, _window_frac, _prot_frac
    a = _EWMA_ALPHA
    _hit_ewma = (1 - a) * _hit_ewma + a * (1.0 if is_hit else 0.0)
    _miss_ewma = (1 - a) * _miss_ewma + a * (0.0 if is_hit else 1.0)

    # Simple scan detection: many misses, few hits
    scanning = (_miss_ewma > 0.75 and _hit_ewma < 0.2)
    _scan_mode = scanning

    # Adapt window and protected fractions
    if _scan_mode:
        _window_frac = 0.02   # keep window tiny during scans
        _prot_frac = 0.90     # keep main protected large
    else:
        # If good locality, expand window a bit to capture recency
        if _hit_ewma > 0.5:
            _window_frac = 0.20
            _prot_frac = 0.80
        elif _hit_ewma > 0.3:
            _window_frac = 0.12
            _prot_frac = 0.80
        else:
            _window_frac = 0.08
            _prot_frac = 0.85


def _get_lru(od):
    """Return LRU key from OrderedDict or None."""
    for k in od.keys():
        return k
    return None


def _move_to_W(key):
    if key in _P_prob:
        _P_prob.pop(key, None)
    if key in _S_prot:
        _S_prot.pop(key, None)
    _W_window.pop(key, None)
    _W_window[key] = True
    _seg[key] = 'W'


def _move_to_P(key):
    if key in _W_window:
        _W_window.pop(key, None)
    if key in _S_prot:
        _S_prot.pop(key, None)
    _P_prob.pop(key, None)
    _P_prob[key] = True
    _seg[key] = 'P'


def _move_to_S(key):
    if key in _W_window:
        _W_window.pop(key, None)
    if key in _P_prob:
        _P_prob.pop(key, None)
    _S_prot.pop(key, None)
    _S_prot[key] = True
    _seg[key] = 'S'


def _rebalance_protected():
    """Keep S within target fraction of main (P+S) by demoting to P."""
    main_sz = len(_P_prob) + len(_S_prot)
    if main_sz <= 0:
        return
    target_S = int(max(1, round(_prot_frac * main_sz)))
    while len(_S_prot) > target_S:
        lru = _get_lru(_S_prot)
        if lru is None:
            break
        _S_prot.pop(lru, None)
        _P_prob[lru] = True
        _seg[lru] = 'P'


def evict(cache_snapshot, obj):
    """
    W-TinyLFU guided victim selection:
    - Prefer window victim during scans.
    - When window >= target: compare f(obj) vs f(P.LRU) to decide if admitting to main.
      * If f(obj) > f(P.LRU), evict from P (admit new to main).
      * Else evict from W (keep main intact).
    - Otherwise prefer evicting from P; if empty, from W; then from S as last resort.
    """
    _ensure_params(cache_snapshot)
    _maybe_age(cache_snapshot)

    key_in = obj.key if obj is not None else None
    f_x = _freq_of(key_in)

    # Targets
    window_target = max(1, int(round(_window_frac * _cap_est)))

    # Candidates
    vW = _get_lru(_W_window)
    vP = _get_lru(_P_prob)
    vS = _get_lru(_S_prot)

    # Scan bias: favor evicting from window to avoid polluting main
    if _scan_mode and vW is not None:
        _admit_plan[key_in] = 'window'
        return vW or vP or vS

    # Window full: perform TinyLFU admission comparison against P's LRU
    if len(_W_window) >= window_target and vW is not None and vP is not None:
        if f_x > _freq_of(vP):
            # Admit new item into main; evict from probation
            _admit_plan[key_in] = 'main'
            return vP
        else:
            # Keep main intact; evict from window
            _admit_plan[key_in] = 'window'
            return vW

    # Otherwise, choose probation victim if exists, guided by frequency
    if vP is not None:
        # If incoming clearly hotter than probation LRU, plan to admit to main
        if f_x >= _freq_of(vP):
            _admit_plan[key_in] = 'main'
        else:
            _admit_plan[key_in] = 'window'
        return vP

    # If no probation, evict from window if possible
    if vW is not None:
        _admit_plan[key_in] = 'window'
        return vW

    # Last resort: evict from protected LRU
    if vS is not None:
        _admit_plan[key_in] = 'main'
        return vS

    # Fallback
    keys = list(cache_snapshot.cache.keys())
    if not keys:
        return None
    _admit_plan[key_in] = 'window'
    return keys[0]


def update_after_hit(cache_snapshot, obj):
    """
    On hit:
    - Update EWMAs and decay frequencies periodically.
    - Increment TinyLFU frequency.
    - In W: refresh MRU; optionally fast-track to S if frequency is high.
    - In P: promote to S.
    - In S: refresh MRU.
    - Rebalance protected size.
    """
    _ensure_params(cache_snapshot)
    _maybe_age(cache_snapshot)
    _update_signals(True, cache_snapshot)

    k = obj.key
    _inc_freq(k)

    if k in _W_window:
        # Refresh MRU in window; if clearly hot, promote to protected
        _W_window.move_to_end(k, last=True)
        if _freq_of(k) >= 3 and not _scan_mode:
            _move_to_S(k)
    elif k in _P_prob:
        # Two-touch: probation -> protected
        _move_to_S(k)
    elif k in _S_prot:
        # Refresh MRU
        _S_prot.move_to_end(k, last=True)
    else:
        # Unknown metadata: place into window MRU
        _move_to_W(k)

    _rebalance_protected()


def update_after_insert(cache_snapshot, obj):
    """
    On miss/insert:
    - Update EWMAs and age frequencies.
    - Increment TinyLFU frequency for the key.
    - Use prior admission plan: 'main' => insert into P; else into W.
    - If scanning, bias new items to window LRU to shed quickly.
    """
    _ensure_params(cache_snapshot)
    _maybe_age(cache_snapshot)
    _update_signals(False, cache_snapshot)

    k = obj.key
    _inc_freq(k)

    plan = _admit_plan.pop(k, 'window')
    if plan == 'main' and not _scan_mode:
        _move_to_P(k)
    else:
        _move_to_W(k)
        # Bias to LRU when scanning to shed quickly
        if _scan_mode:
            # Move to LRU position
            try:
                _W_window.move_to_end(k, last=False)
            except Exception:
                pass

    _rebalance_protected()


def update_after_evict(cache_snapshot, obj, evicted_obj):
    """
    After eviction:
    - Remove evicted key from its segment and metadata.
    - No ghosts are kept; TinyLFU retains decayed history implicitly.
    """
    _ensure_params(cache_snapshot)
    k = evicted_obj.key

    if k in _W_window:
        _W_window.pop(k, None)
    if k in _P_prob:
        _P_prob.pop(k, None)
    if k in _S_prot:
        _S_prot.pop(k, None)
    _seg.pop(k, None)
    _freq.pop(k, None)

    # If an admission plan existed for the incoming key, keep it for update_after_insert
    # (nothing else to do here)
# EVOLVE-BLOCK-END

# This part remains fixed (not evolved)
def run_caching(trace_path: str, copy_code_dst: str):
    """Run the caching algorithm on a trace"""
    import os
    with open(os.path.abspath(__file__), 'r', encoding="utf-8") as f:
        code_str = f.read()
    with open(os.path.join(copy_code_dst), 'w') as f:
        f.write(code_str)
    from cache_utils import Cache, CacheConfig, CacheObj, Trace
    trace = Trace(trace_path=trace_path)
    cache_capacity = max(int(trace.get_ndv() * 0.1), 1)
    cache = Cache(CacheConfig(cache_capacity))
    for entry in trace.entries:
        obj = CacheObj(key=str(entry.key))
        cache.get(obj)
    with open(copy_code_dst, 'w') as f:
        f.write("")
    hit_rate = round(cache.hit_count / cache.access_count, 6)
    return hit_rate