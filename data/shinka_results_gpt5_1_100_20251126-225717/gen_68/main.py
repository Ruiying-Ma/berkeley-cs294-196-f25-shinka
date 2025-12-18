# EVOLVE-BLOCK-START
"""Cache eviction algorithm for optimizing hit rates across multiple workloads"""

from collections import OrderedDict

# Segmented LRU (SLRU) metadata:
# - _probation: objects seen once (newly inserted), LRU-ordered
# - _protected: objects that have been hit (promoted), LRU-ordered
# We adapt the protected fraction slightly based on runtime signals.
_probation = OrderedDict()   # key -> None (value unused)
_protected = OrderedDict()   # key -> None (value unused)
_key_seg = dict()            # key -> 'prob' or 'prot'

# Ghost histories to recognize re-references to recently evicted items
_ghost_probation = OrderedDict()  # key -> epoch of ghost insertion
_ghost_protected = OrderedDict()  # key -> epoch of ghost insertion
_GHOST_LIMIT_MULT = 2

# TinyLFU-like lightweight decayed frequency
_refcnt = {}                 # key -> (count, epoch)
_epoch = 0
_last_epoch_tick = 0
_DECAY_WINDOW = 128          # accesses between epochs; adjusted using capacity
_last_victim_score = 0.0

_PROTECTED_FRAC = 0.8        # target fraction of cache to allocate to protected segment
_ADAPT_STEP = 0.02           # step used to adjust protected fraction within [0.05, 0.95]

# Scan-resistance and two-touch gating (epoch-scoped, low overhead)
_scan_mode = False
_scan_mode_epochs_left = 0
_epoch_unique = set()        # unique keys observed within the current decay window
_touched_once = {}           # key -> epoch when first touched in probation (for two-touch in scan mode)

# Sampling parameters for LRFU-like victim selection
_BASE_PROB_SAMPLE = 3
_BASE_PROT_SAMPLE = 2


def _increase_protected():
    global _PROTECTED_FRAC
    _PROTECTED_FRAC = min(0.95, _PROTECTED_FRAC + _ADAPT_STEP)


def _decrease_protected():
    global _PROTECTED_FRAC
    _PROTECTED_FRAC = max(0.05, _PROTECTED_FRAC - _ADAPT_STEP)


def _ensure_params(cache_snapshot):
    """Initialize/adjust parameters that depend on capacity."""
    global _DECAY_WINDOW
    cap = max(cache_snapshot.capacity, 1)
    # Tie decay window to capacity to align half-life with working-set size
    _DECAY_WINDOW = max(64, cap)


def _maybe_age(cache_snapshot):
    """Advance epoch based on access_count, and trim ghost lists and manage scan mode."""
    global _epoch, _last_epoch_tick, _scan_mode, _scan_mode_epochs_left, _epoch_unique
    _ensure_params(cache_snapshot)
    if cache_snapshot.access_count - _last_epoch_tick >= _DECAY_WINDOW:
        # Evaluate the last window's uniqueness and hit rate to detect scans
        window = max(1, _DECAY_WINDOW)
        unique_density = min(1.0, len(_epoch_unique) / float(window))
        hit_rate = cache_snapshot.hit_count / max(1, float(cache_snapshot.access_count))
        if unique_density > 0.7 and hit_rate < 0.2:
            _scan_mode = True
            _scan_mode_epochs_left = 1
            # During scans, lean away from protected a bit
            _decrease_protected()
        else:
            if _scan_mode_epochs_left > 0:
                _scan_mode_epochs_left -= 1
            _scan_mode = _scan_mode_epochs_left > 0
        # Reset unique tracker for the new window
        _epoch_unique.clear()

        _epoch += 1
        _last_epoch_tick = cache_snapshot.access_count
        # Trim ghost histories to bounded size
        limit = max(1, _GHOST_LIMIT_MULT * max(cache_snapshot.capacity, 1))
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
        # decay by halving per epoch (cap max shifts to avoid zeroing too fast)
        c = c >> min(6, de)
    return max(0, c)


def _inc(key):
    """Increment decayed frequency count for a key."""
    c, e = _refcnt.get(key, (0, _epoch))
    if e != _epoch:
        c = c >> min(6, _epoch - e)
        e = _epoch
    # cap growth to avoid overflow
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


def _rebalance(cache_snapshot):
    """Demote protected LRU entries if protected size exceeds target."""
    total = len(cache_snapshot.cache)
    if total <= 0:
        return
    target = max(1, int(total * _PROTECTED_FRAC))

    while len(_protected) > target:
        # Demote protected LRU to probation MRU
        k, _ = _protected.popitem(last=False)
        _probation[k] = None
        _key_seg[k] = 'prob'


def _sample_lru_keys(od, n):
    """Return up to n keys from the LRU side (front) of an OrderedDict."""
    res = []
    it = iter(od.keys())
    for i in range(n):
        try:
            res.append(next(it))
        except StopIteration:
            break
    return res


def _victim_tuple(key, seg, rec_idx):
    """Tuple for comparing eviction candidates. Lower is colder."""
    # Prefer lower frequency; tie-break by segment (probation preferred), then by older recency (smaller index)
    return (_score(key), 0 if seg == 'prob' else 1, rec_idx, key)


def evict(cache_snapshot, obj):
    '''
    This function defines how the algorithm chooses the eviction victim.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The new object that needs to be inserted into the cache.
    - Return:
        - `candid_obj_key`: The key of the cached object that will be evicted to make room for `obj`.
    '''
    # Keep metadata consistent and properly segmented before choosing a victim
    _maybe_age(cache_snapshot)
    _sync_metadata(cache_snapshot)
    _rebalance(cache_snapshot)

    # Track key for scan detection unique density
    try:
        _epoch_unique.add(obj.key)
    except Exception:
        pass

    # Clean up stale entries (if any)
    for k in list(_probation.keys()):
        if k not in cache_snapshot.cache:
            _probation.pop(k, None)
            _key_seg.pop(k, None)
    for k in list(_protected.keys()):
        if k not in cache_snapshot.cache:
            _protected.pop(k, None)
            _key_seg.pop(k, None)

    # If metadata is empty, fallback
    if not cache_snapshot.cache:
        return None

    # Utility: choose victim from a segment by sampling a few LRU candidates and picking coldest by (score, age, recency)
    def _pick_from_segment(seg_od, sample_n, seg_name):
        keys = _sample_lru_keys(seg_od, sample_n)
        best = None
        for idx, key in enumerate(keys):
            if key not in cache_snapshot.cache:
                continue
            # Age in epochs since last count update
            ce = _refcnt.get(key, (0, _epoch))
            age = max(0, _epoch - ce[1])
            # Penalize older items slightly to prefer truly stale entries
            tup = (_score(key), 0 if seg_name == 'prob' else 1, idx + min(50, age * 2), key)
            if best is None or tup < best:
                best = tup
        return best[3] if best is not None else None

    # Ghost-driven overrides (ARC-like bias)
    g_prot_epoch = _ghost_protected.get(obj.key)
    g_prob_epoch = _ghost_probation.get(obj.key)
    recent_prot_ghost = (g_prot_epoch is not None) and (_epoch - g_prot_epoch <= 2)
    recent_prob_ghost = (g_prob_epoch is not None) and (_epoch - g_prob_epoch <= 2)

    # Strong scan handling: prefer evicting from probation during scans
    if _scan_mode and _probation:
        cand = _pick_from_segment(_probation, 3, 'prob')
        if cand is not None:
            return cand

    # If incoming was recently in protected ghost, free from probation to re-protect quickly
    if recent_prot_ghost and _probation:
        cand = _pick_from_segment(_probation, 3, 'prob')
        if cand is not None:
            return cand

    # If incoming was recently in probation ghost and protected is non-empty, free from protected
    if recent_prob_ghost and _protected:
        cand = _pick_from_segment(_protected, 2, 'prot')
        if cand is not None:
            return cand

    # Adaptive sampling sizes
    prob_sample = _BASE_PROB_SAMPLE
    prot_sample = _BASE_PROT_SAMPLE
    if _scan_mode:
        prob_sample += 1
        prot_sample = max(1, prot_sample - 1)
    if len(_probation) > len(_protected) + 2:
        prob_sample += 1
    if len(_protected) > len(_probation) + 5:
        prot_sample += 1

    # Build candidate tuples from both segments' LRU sides with age-aware recency
    candidates = []
    # Probation candidates
    pkeys = _sample_lru_keys(_probation, prob_sample)
    for idx, pk in enumerate(pkeys):
        if pk in cache_snapshot.cache:
            ce = _refcnt.get(pk, (0, _epoch))
            age = max(0, _epoch - ce[1])
            candidates.append((_score(pk), 0, idx + min(50, age * 2), pk))
    # Protected candidates
    tkeys = _sample_lru_keys(_protected, prot_sample)
    for idx, tk in enumerate(tkeys):
        if tk in cache_snapshot.cache:
            ce = _refcnt.get(tk, (0, _epoch))
            age = max(0, _epoch - ce[1])
            candidates.append((_score(tk), 1, idx + min(50, age * 2), tk))

    if not candidates:
        # Fallback: pick any key from cache if metadata got desynced
        return next(iter(cache_snapshot.cache.keys()))

    # Choose coldest by (score, segment preference, age-biased recency)
    best = min(candidates)
    candid_obj_key = best[3]
    victim_from_protected = (_key_seg.get(candid_obj_key) == 'prot')

    # Adaptive tuning: if we are forced to evict from protected, reduce its target fraction slightly
    if victim_from_protected:
        _decrease_protected()

    return candid_obj_key


def update_after_hit(cache_snapshot, obj):
    '''
    This function defines how the algorithm update the metadata it maintains immediately after a cache hit.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The object accessed during the cache hit.
    - Return: `None`
    '''
    _maybe_age(cache_snapshot)
    _sync_metadata(cache_snapshot)

    k = obj.key
    _inc(k)
    try:
        _epoch_unique.add(k)
    except Exception:
        pass

    seg = _key_seg.get(k)

    # Determine if locality is poor: low hit rate or high uniqueness in window
    window = max(1, _DECAY_WINDOW)
    try:
        current_hit_rate = cache_snapshot.hit_count / max(1.0, float(cache_snapshot.access_count))
    except Exception:
        current_hit_rate = 0.0
    unique_density = min(1.0, len(_epoch_unique) / float(window))
    use_two_touch = _scan_mode or (current_hit_rate < 0.25) or (unique_density > 0.6)

    s = _score(k)
    recent_prot_ghost = (_ghost_protected.get(k) is not None) and (_epoch - _ghost_protected.get(k, _epoch) <= 4)

    if seg == 'prob':
        if use_two_touch and not recent_prot_ghost and s < 2:
            # Time-bounded two-touch gating: second touch within one epoch promotes
            last = _touched_once.get(k)
            if last is not None and (_epoch - last) <= 1:
                _touched_once.pop(k, None)
                _probation.pop(k, None)
                _protected[k] = None  # MRU
                _key_seg[k] = 'prot'
            else:
                _touched_once[k] = _epoch
                # Refresh to MRU of probation
                if k in _probation:
                    _probation.move_to_end(k, last=True)
                else:
                    _probation[k] = None
        else:
            # Promote to protected when locality is decent OR when clearly hot via freq/ghost signal
            _probation.pop(k, None)
            _protected[k] = None  # inserted at MRU
            _key_seg[k] = 'prot'
            _increase_protected()  # hits in probation signal benefit from a larger protected segment
    elif seg == 'prot':
        # Refresh recency in protected
        if k in _protected:
            _protected.move_to_end(k, last=True)
        else:
            # If somehow missing from the structure, reinsert into protected
            _protected[k] = None
            _key_seg[k] = 'prot'
        # Clear any stale two-touch marker
        _touched_once.pop(k, None)
        # If protected is much smaller than probation, slightly expand it
        if len(_protected) * 2 < max(1, len(_probation)):
            _increase_protected()
    else:
        # Unknown key (shouldn't happen on hit).
        # In poor locality, keep in probation with refresh; otherwise, promote.
        if use_two_touch and s < 2:
            _probation[k] = None
            _key_seg[k] = 'prob'
            _probation.move_to_end(k, last=True)
        else:
            _probation[k] = None
            _key_seg[k] = 'prob'
            _probation.pop(k, None)
            _protected[k] = None
            _key_seg[k] = 'prot'
            _increase_protected()

    _rebalance(cache_snapshot)


def update_after_insert(cache_snapshot, obj):
    '''
    This function defines how the algorithm updates the metadata it maintains immediately after inserting a new object into the cache.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The object that was just inserted into the cache.
    - Return: `None`
    '''
    _maybe_age(cache_snapshot)
    _sync_metadata(cache_snapshot)
    k = obj.key
    _inc(k)
    try:
        _epoch_unique.add(k)
    except Exception:
        pass

    # Decide segment placement using ghost history (segment-aware)
    g_prot_epoch = _ghost_protected.get(k)
    g_prob_epoch = _ghost_probation.get(k)
    recent_prot_ghost = (g_prot_epoch is not None) and (_epoch - g_prot_epoch <= 4)
    recent_prob_ghost = (g_prob_epoch is not None) and (_epoch - g_prob_epoch <= 4)

    # Reset any existing placement
    if _key_seg.get(k) == 'prot':
        _protected.pop(k, None)
    else:
        _probation.pop(k, None)

    # Locality signal from current window
    window = max(1, _DECAY_WINDOW)
    try:
        current_hit_rate = cache_snapshot.hit_count / max(1.0, float(cache_snapshot.access_count))
    except Exception:
        current_hit_rate = 0.0
    unique_density = min(1.0, len(_epoch_unique) / float(window))
    high_uniqueness = unique_density > 0.6
    poor_locality = current_hit_rate < 0.25 or high_uniqueness

    s = _score(k)

    if _scan_mode:
        # Scan resistance: insert at probation LRU to minimize pollution,
        # require two-touch before promotion via update_after_hit.
        _probation[k] = None
        _probation.move_to_end(k, last=False)  # LRU side
        _key_seg[k] = 'prob'
    else:
        if recent_prot_ghost or s >= 3:
            # Re-admit into protected due to recent protected ghost hit or strong frequency
            _protected[k] = None
            _key_seg[k] = 'prot'
            _increase_protected()
        else:
            # Default admission into probation
            _probation[k] = None
            _key_seg[k] = 'prob'
            if recent_prob_ghost:
                # Slight boost for recently evicted probation ghost to accelerate useful re-references
                _probation.move_to_end(k, last=True)  # ensure MRU
                _inc(k)
            elif poor_locality and s == 0:
                # Bias towards LRU position when locality is poor and item is cold
                _probation.move_to_end(k, last=False)
            else:
                # Keep MRU position
                _probation.move_to_end(k, last=True)

    _rebalance(cache_snapshot)


def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    This function defines how the algorithm updates the metadata it maintains immediately after evicting the victim.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The object to be inserted into the cache.
        - `evicted_obj`: The object that was just evicted from the cache.
    - Return: `None`
    '''
    _maybe_age(cache_snapshot)
    # Record into ghost according to the segment it was evicted from
    k = evicted_obj.key
    seg = _key_seg.get(k, None)
    # Store last victim score for potential future enhancements
    global _last_victim_score
    _last_victim_score = _score(k)

    if seg == 'prob':
        _ghost_probation[k] = _epoch
        _probation.pop(k, None)
    elif seg == 'prot':
        _ghost_protected[k] = _epoch
        _protected.pop(k, None)

    # Clean any stale two-touch markers
    _touched_once.pop(k, None)

    _key_seg.pop(k, None)

    # Trim ghost histories to bounded size
    limit = max(1, _GHOST_LIMIT_MULT * max(cache_snapshot.capacity, 1))
    while len(_ghost_probation) > limit:
        _ghost_probation.popitem(last=False)
    while len(_ghost_protected) > limit:
        _ghost_protected.popitem(last=False)

    # Adaptive protected tuning:
    try:
        new_score = _score(obj.key)
        if seg == 'prob' and new_score > _last_victim_score:
            # Incoming appears hotter than evicted probation item: expand protected a bit
            _increase_protected()
        elif seg == 'prot' and new_score <= _last_victim_score:
            # Evicted from protected for an item no hotter: shrink protected slightly
            _decrease_protected()
    except Exception:
        pass

    # After an eviction, ensure protected segment still respects target (it might shrink due to target change)
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