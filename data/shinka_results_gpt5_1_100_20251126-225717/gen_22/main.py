# EVOLVE-BLOCK-START
"""Cache eviction algorithm for optimizing hit rates across multiple workloads"""

from collections import OrderedDict

# Segmented LRU (SLRU/ARC-like):
# - _probation (T1): objects seen once, LRU-ordered
# - _protected (T2): objects seen at least twice, LRU-ordered
_probation = OrderedDict()   # key -> None
_protected = OrderedDict()   # key -> None
_key_seg = dict()            # key -> 'prob' or 'prot'

# Ghost histories (ARC-style):
# - _ghost_probation (B1): recently evicted from T1
# - _ghost_protected (B2): recently evicted from T2
_ghost_probation = OrderedDict()  # key -> epoch
_ghost_protected = OrderedDict()  # key -> epoch
_GHOST_LIMIT_MULT = 2

# TinyLFU-like decayed frequency counters
_refcnt = {}                 # key -> (count, epoch)
_epoch = 0
_last_epoch_tick = 0
_DECAY_WINDOW = 128

# ARC p-target (size of T1) with momentum smoothing
_p_target = 0.0              # target size for T1, in entries
_p_momentum = 0.0

# Scan detector sliding window
_W_MULT = 2
_win_start = 0
_win_total = 0
_win_hits = 0
_win_seen = set()
_scan_until = 0  # access_count until which scan mode is active


def _clamp(x, lo, hi):
    return lo if x < lo else hi if x > hi else x


def _ensure_params(cache_snapshot):
    """Initialize/adjust parameters tied to capacity and p-target."""
    global _DECAY_WINDOW, _p_target
    cap = max(getattr(cache_snapshot, "capacity", 1), 1)
    _DECAY_WINDOW = max(64, cap)
    # Lazy init p_target to half capacity on first use
    if _p_target <= 0.0:
        _p_target = cap * 0.5


def _maybe_age(cache_snapshot):
    """Advance epoch based on access_count, and trim ghost lists."""
    global _epoch, _last_epoch_tick
    _ensure_params(cache_snapshot)
    if cache_snapshot.access_count - _last_epoch_tick >= _DECAY_WINDOW:
        _epoch += 1
        _last_epoch_tick = cache_snapshot.access_count
        _trim_ghosts(cache_snapshot)


def _trim_ghosts(cache_snapshot):
    """Keep ghost histories bounded."""
    limit = max(1, _GHOST_LIMIT_MULT * max(getattr(cache_snapshot, "capacity", 1), 1))
    while len(_ghost_probation) > limit:
        _ghost_probation.popitem(last=False)
    while len(_ghost_protected) > limit:
        _ghost_protected.popitem(last=False)


def _score(key):
    """Return decayed frequency score for a key."""
    ce = _refcnt.get(key)
    if ce is None:
        return 0
    c, e = ce
    de = _epoch - e
    if de > 0:
        c = c >> min(6, de)  # halve up to 6 times
    return max(0, c)


def _inc(key):
    """Increment decayed frequency count for a key."""
    c, e = _refcnt.get(key, (0, _epoch))
    if e != _epoch:
        c = c >> min(6, _epoch - e)
        e = _epoch
    c = min(c + 1, 1 << 30)
    _refcnt[key] = (c, e)


def _sync_metadata(cache_snapshot):
    """Ensure SLRU metadata matches current cache content."""
    cached_keys = set(cache_snapshot.cache.keys())

    # Remove entries no longer in cache
    for k in list(_key_seg.keys()):
        if k not in cached_keys:
            if _key_seg.get(k) == 'prob':
                _probation.pop(k, None)
            else:
                _protected.pop(k, None)
            _key_seg.pop(k, None)

    # Add cached keys missing from metadata into probation MRU
    for k in cached_keys:
        if k not in _key_seg:
            _probation[k] = None
            _key_seg[k] = 'prob'


def _target_protected(total):
    """Compute target protected count based on p-target."""
    t1 = int(round(_clamp(_p_target, 0.0, float(total))))
    return max(0, total - t1)


def _rebalance(cache_snapshot):
    """Demote protected LRU entries if protected size exceeds target (ARC-style)."""
    total = len(cache_snapshot.cache)
    if total <= 0:
        return
    target_prot = _target_protected(total)
    while len(_protected) > target_prot:
        k, _ = _protected.popitem(last=False)  # LRU from T2 demoted to T1 MRU
        _probation[k] = None
        _key_seg[k] = 'prob'


def _in_scan_mode(cache_snapshot):
    return cache_snapshot.access_count < _scan_until


def _window_update(cache_snapshot, is_hit, key):
    """Update sliding window stats and detect scans."""
    global _win_start, _win_total, _win_hits, _win_seen, _scan_until
    cap = max(getattr(cache_snapshot, "capacity", 1), 1)
    W = max(2, _W_MULT * cap)

    if _win_start == 0:
        _win_start = cache_snapshot.access_count

    _win_total += 1
    if is_hit:
        _win_hits += 1
    _win_seen.add(key)

    if (cache_snapshot.access_count - _win_start) >= W:
        hit_rate = _win_hits / max(1, _win_total)
        unique_rate = len(_win_seen) / max(1, _win_total)
        if unique_rate > 0.6 and hit_rate < 0.2:
            _scan_until = cache_snapshot.access_count + W
        # reset window
        _win_start = cache_snapshot.access_count
        _win_total = 0
        _win_hits = 0
        _win_seen.clear()


def _adjust_p_on_ghost(cache_snapshot, hit_b1, hit_b2, ghost_epoch):
    """Adjust ARC p-target with momentum based on ghost hit type and freshness."""
    global _p_target, _p_momentum
    cap = max(getattr(cache_snapshot, "capacity", 1), 1)
    if not (hit_b1 or hit_b2):
        return
    # Freshness weight based on age in epochs
    age = max(0, _epoch - (ghost_epoch if ghost_epoch is not None else _epoch))
    if age <= 1:
        fresh_w = 1.5
    elif age <= 3:
        fresh_w = 1.0
    else:
        fresh_w = 0.75

    b1 = len(_ghost_probation)
    b2 = len(_ghost_protected)
    if hit_b1:
        base = max(1.0, (b2 / max(1.0, b1)))
        step = min(base * fresh_w, 0.25 * cap)
        sign = +1.0
    else:
        base = max(1.0, (b1 / max(1.0, b2)))
        step = min(base * fresh_w, 0.25 * cap)
        sign = -1.0

    _p_momentum = 0.5 * _p_momentum + sign * step
    _p_target = _clamp(_p_target + _p_momentum, 0.0, float(cap))


def evict(cache_snapshot, obj):
    '''
    Choose an eviction victim using ARC-style p-target, TinyLFU signal, and scan handling.
    '''
    _maybe_age(cache_snapshot)
    _sync_metadata(cache_snapshot)
    _rebalance(cache_snapshot)

    # Pre-adjust p based on the incoming object's ghost presence (proactive ARC response)
    g_b1 = _ghost_probation.get(obj.key, None)
    g_b2 = _ghost_protected.get(obj.key, None)
    if g_b1 is not None or g_b2 is not None:
        _adjust_p_on_ghost(cache_snapshot, hit_b1=(g_b1 is not None), hit_b2=(g_b2 is not None),
                           ghost_epoch=(g_b1 if g_b1 is not None else g_b2))

    # Clean up metadata if any keys are stale
    for k in list(_probation.keys()):
        if k not in cache_snapshot.cache:
            _probation.pop(k, None)
            _key_seg.pop(k, None)
    for k in list(_protected.keys()):
        if k not in cache_snapshot.cache:
            _protected.pop(k, None)
            _key_seg.pop(k, None)

    # Determine LRU candidates
    prob_lru = next(iter(_probation.keys()), None)
    prot_lru = next(iter(_protected.keys()), None)

    if prob_lru is None and prot_lru is None:
        # Fallback
        return next(iter(cache_snapshot.cache.keys())) if cache_snapshot.cache else None

    cap = max(getattr(cache_snapshot, "capacity", 1), 1)
    total = len(cache_snapshot.cache)
    t1_target = int(round(_clamp(_p_target, 0.0, float(cap))))
    victim_key = None

    # Scan mode: prioritize evicting from T1 to protect T2
    if _in_scan_mode(cache_snapshot):
        if prob_lru is not None:
            victim_key = prob_lru
        else:
            victim_key = prot_lru
        # Push p high during scan to keep evicting from T1
        global _p_target
        _p_target = _clamp(max(_p_target, 0.9 * cap), 0.0, float(cap))
        return victim_key

    # Normal ARC policy with TinyLFU sampling
    if prob_lru is None:
        victim_key = prot_lru
    elif prot_lru is None:
        victim_key = prob_lru
    else:
        # If T1 above target or T2 below target, evict from T1; otherwise from T2.
        if (len(_probation) > t1_target) or (len(_protected) < _target_protected(total)):
            victim_key = prob_lru
        else:
            # Consider TinyLFU scores to pick the colder LRU when both segments are tight
            s_prob = _score(prob_lru)
            s_prot = _score(prot_lru)
            # Evict from protected only if clearly colder
            if s_prot + 1 < s_prob:
                victim_key = prot_lru
            else:
                victim_key = prob_lru

    return victim_key


def update_after_hit(cache_snapshot, obj):
    '''
    Update metadata after a cache hit: promotions, recency, counters, and scan/window stats.
    '''
    _maybe_age(cache_snapshot)
    _sync_metadata(cache_snapshot)
    _window_update(cache_snapshot, is_hit=True, key=obj.key)

    k = obj.key
    _inc(k)
    seg = _key_seg.get(k)

    if seg == 'prob':
        # In scan mode, require two touches before promotion
        if _in_scan_mode(cache_snapshot) and _score(k) < 2:
            # Just refresh in T1 (move to MRU)
            if k in _probation:
                _probation.move_to_end(k, last=True)
            else:
                _probation[k] = None
            _key_seg[k] = 'prob'
        else:
            # Promote to protected on hit
            _probation.pop(k, None)
            _protected[k] = None  # MRU
            _key_seg[k] = 'prot'
    elif seg == 'prot':
        # Refresh recency in protected
        if k in _protected:
            _protected.move_to_end(k, last=True)
        else:
            _protected[k] = None
            _key_seg[k] = 'prot'
    else:
        # Unknown key (shouldn't happen) – put into probation and handle as first hit
        _probation[k] = None
        _key_seg[k] = 'prob'
        # Promote if not in scan mode, else defer
        if not _in_scan_mode(cache_snapshot):
            _probation.pop(k, None)
            _protected[k] = None
            _key_seg[k] = 'prot'

    _rebalance(cache_snapshot)


def update_after_insert(cache_snapshot, obj):
    '''
    Update metadata after inserting a new object: admission policy and ARC p-adaptation.
    '''
    _maybe_age(cache_snapshot)
    _sync_metadata(cache_snapshot)
    _window_update(cache_snapshot, is_hit=False, key=obj.key)

    k = obj.key
    _inc(k)

    # Remove any stale placement (shouldn't exist on insert, but safe)
    if _key_seg.get(k) == 'prot':
        _protected.pop(k, None)
    elif _key_seg.get(k) == 'prob':
        _probation.pop(k, None)

    # Ghost-aware admission and p-adjustment
    g_b1 = _ghost_probation.pop(k, None)
    g_b2 = _ghost_protected.pop(k, None)
    hit_b1 = g_b1 is not None
    hit_b2 = g_b2 is not None

    # Adjust p using ARC rule with momentum
    if hit_b1 or hit_b2:
        _adjust_p_on_ghost(cache_snapshot, hit_b1=hit_b1, hit_b2=hit_b2, ghost_epoch=(g_b1 if hit_b1 else g_b2))

    # Admission policy:
    # - In scan mode: always to probation, no direct T2 admission
    # - Else: if recent B2 ghost, admit to protected; otherwise probation
    recent_b2 = hit_b2 and (_epoch - (g_b2 if g_b2 is not None else _epoch) <= 2)

    if _in_scan_mode(cache_snapshot):
        _probation[k] = None
        _key_seg[k] = 'prob'
    elif recent_b2:
        _protected[k] = None
        _key_seg[k] = 'prot'
    else:
        _probation[k] = None
        _key_seg[k] = 'prob'

    _rebalance(cache_snapshot)


def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    Update metadata after eviction: move victim to appropriate ghost and keep bounds.
    '''
    if evicted_obj is None:
        return

    _maybe_age(cache_snapshot)

    k = evicted_obj.key
    seg = _key_seg.get(k, None)

    if seg == 'prob':
        _probation.pop(k, None)
        _ghost_probation[k] = _epoch
    elif seg == 'prot':
        _protected.pop(k, None)
        _ghost_protected[k] = _epoch
    else:
        # Unknown: treat as T1 ghost by default
        _ghost_probation[k] = _epoch

    _key_seg.pop(k, None)

    _trim_ghosts(cache_snapshot)
    _rebalance(cache_snapshot)

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