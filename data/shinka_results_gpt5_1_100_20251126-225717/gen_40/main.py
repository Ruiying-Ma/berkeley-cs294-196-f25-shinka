# EVOLVE-BLOCK-START
"""Cache eviction algorithm for optimizing hit rates across multiple workloads"""

# Hybrid ARC-style segment sizing with HeatGuard victim selection.
# - Two live segments: probation (recency) and protected (frequency).
# - Ghost lists (B1/B2) adapt the protected share like ARC.
# - Victim inside a segment is chosen by "heat" = decayed LFU - age term.
# - Hits promote probation -> protected and bump decayed LFU.
# - Inserts are ghost-aware; seed LFU using last victim's merit to reduce pollution.
# - Evictions record ghosts and update last-victim score.

from math import pow

# General access ledger (key -> last access time)
m_key_timestamp = dict()

# Segmented LRU metadata: key -> last access time
_probation = dict()     # unproven (recency)
_protected = dict()     # reused/frequent (frequency)

# Ghost histories (ARC-style): key -> last eviction time
_ghost_probation = dict()   # B1
_ghost_protected = dict()   # B2

# Adaptive target for protected share (ratio 0..1), start frequency-biased
_prot_ratio = 0.66

# Decayed LFU metadata
_freq = dict()      # key -> decayed score
_freq_ts = dict()   # key -> last score update time
_half_life = None   # decay half-life (in accesses)

# Admission guard
_last_victim_score = 0.0


def _clamp(x, lo, hi):
    return lo if x < lo else hi if x > hi else x


def _ensure_params(cache_snapshot):
    """Initialize parameters that depend on capacity."""
    global _half_life
    cap = max(int(getattr(cache_snapshot, "capacity", 1)), 1)
    if _half_life is None:
        # Scale half-life with capacity to stabilize under larger caches.
        _half_life = max(20, int(1.5 * cap))


def _get_caps(cache_snapshot):
    """Compute target sizes for probation and protected segments using adaptive ratio."""
    global _prot_ratio
    total_cap = max(int(getattr(cache_snapshot, "capacity", 1)), 1)
    # Keep some non-zero portion on both sides when capacity > 1
    pr_lo = 0.1 if total_cap > 1 else 0.0
    pr_hi = 0.9 if total_cap > 1 else 1.0
    prot = int(round(total_cap * _clamp(_prot_ratio, pr_lo, pr_hi)))
    prot = min(max(prot, 1 if total_cap > 1 else 0), total_cap - 1 if total_cap > 1 else 1)
    prob = max(total_cap - prot, 1)
    return prob, prot


def _lru_key_in(seg_dict, cache_snapshot):
    """Return the LRU key from seg_dict that is currently in the cache."""
    min_key = None
    min_ts = None
    cache_keys = cache_snapshot.cache.keys()
    for k, ts in seg_dict.items():
        if k in cache_keys:
            if (min_ts is None) or (ts < min_ts):
                min_ts = ts
                min_key = k
    return min_key


def _lru_key(seg_dict):
    """Return the LRU key from seg_dict irrespective of cache presence."""
    min_key = None
    min_ts = None
    for k, ts in seg_dict.items():
        if (min_ts is None) or (ts < min_ts):
            min_ts = ts
            min_key = k
    return min_key


def _trim_ghosts(cache_snapshot):
    """Keep ghost histories bounded to avoid memory growth."""
    total_cap = max(int(getattr(cache_snapshot, "capacity", 1)), 1)
    limit = max(2 * total_cap, 1)
    while (len(_ghost_probation) + len(_ghost_protected)) > limit:
        if len(_ghost_probation) >= len(_ghost_protected):
            k = _lru_key(_ghost_probation)
            if k is None:
                break
            _ghost_probation.pop(k, None)
        else:
            k = _lru_key(_ghost_protected)
            if k is None:
                break
            _ghost_protected.pop(k, None)


def _decayed_score(key, now):
    """Lazily apply exponential decay to the LFU score."""
    s = _freq.get(key, 0.0)
    t = _freq_ts.get(key, now)
    dt = now - t
    if dt > 0 and _half_life and _half_life > 0:
        s *= pow(0.5, dt / float(_half_life))
    return s


def _set_score(key, score, now):
    _freq[key] = score
    _freq_ts[key] = now


def _heat(key, seg, now, cap):
    """
    Higher is hotter; victim is minimum heat.
    Heat blends decayed LFU (keep) vs. age (evict). Segment biases recency vs. frequency.
    """
    ts = _protected.get(key) if seg == 'prot' else _probation.get(key)
    if ts is None:
        ts = m_key_timestamp.get(key, now)
    age = now - ts
    score = _decayed_score(key, now)
    lam = 1.0 / max(1, cap)
    if seg == 'prob':
        # Emphasize recency; small credit for score
        return 0.6 * score - 1.2 * lam * age
    else:
        # Protected: emphasize frequency; still consider recency
        return 1.2 * score - 0.6 * lam * age


def _pick_coldest(seg_dict, seg_name, cache_snapshot):
    """Pick the coldest (min heat) key in a given segment among actual cache keys."""
    now = cache_snapshot.access_count
    cap = max(int(getattr(cache_snapshot, "capacity", 1)), 1)
    cache_keys = cache_snapshot.cache.keys()
    best_k = None
    best_h = None
    for k in seg_dict.keys():
        if k in cache_keys:
            h = _heat(k, 'prot' if seg_name == 'prot' else 'prob', now, cap)
            if best_h is None or h < best_h:
                best_h = h
                best_k = k
    return best_k


def _demote_until_quota(cache_snapshot):
    """Demote coldest protected to probation until protected size within target."""
    _, prot_cap = _get_caps(cache_snapshot)
    if len(_protected) <= prot_cap:
        return
    while len(_protected) > prot_cap:
        dk = _pick_coldest(_protected, 'prot', cache_snapshot)
        if dk is None:
            dk = _lru_key_in(_protected, cache_snapshot)
            if dk is None:
                break
        dt = _protected.pop(dk, m_key_timestamp.get(dk, cache_snapshot.access_count))
        _probation[dk] = dt


def evict(cache_snapshot, obj):
    '''
    This function defines how the algorithm chooses the eviction victim.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The new object that needs to be inserted into the cache.
    - Return:
        - `candid_obj_key`: The key of the cached object that will be evicted to make room for `obj`.
    '''
    global _prot_ratio
    _ensure_params(cache_snapshot)
    prob_cap, prot_cap = _get_caps(cache_snapshot)
    cap = max(int(getattr(cache_snapshot, "capacity", 1)), 1)

    # Ghost-guided adaptation: ARC-like feedback
    g_prot = obj.key in _ghost_protected
    g_prob = obj.key in _ghost_probation
    if g_prot and not g_prob:
        _prot_ratio = _clamp(_prot_ratio + 0.1, 0.1, 0.9)
    elif g_prob and not g_prot:
        _prot_ratio = _clamp(_prot_ratio - 0.1, 0.1, 0.9)
    # Recompute targets after adjustment
    prob_cap, prot_cap = _get_caps(cache_snapshot)

    # Select eviction segment
    victim_key = None
    if g_prot and len(_probation) > 0:
        # Favor frequency: evict from probation
        victim_key = _pick_coldest(_probation, 'prob', cache_snapshot)
    elif g_prob and len(_protected) > 0:
        # Favor recency: evict from protected
        victim_key = _pick_coldest(_protected, 'prot', cache_snapshot)
    else:
        # Choose segment exceeding target; default to probation
        if len(_probation) > 0 and (len(_probation) >= prob_cap or len(_protected) == 0):
            victim_key = _pick_coldest(_probation, 'prob', cache_snapshot)
        if victim_key is None and len(_protected) > 0:
            victim_key = _pick_coldest(_protected, 'prot', cache_snapshot)

    if victim_key is None:
        # Fallback: pick coldest overall using probation-like heat
        cache_keys = list(cache_snapshot.cache.keys())
        if cache_keys:
            now = cache_snapshot.access_count
            victim_key = min(
                cache_keys,
                key=lambda k: (_decayed_score(k, now) - (now - m_key_timestamp.get(k, now)) / max(2, cap))
            )
        else:
            return None
    return victim_key


def update_after_hit(cache_snapshot, obj):
    '''
    This function defines how the algorithm update the metadata it maintains immediately after a cache hit.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The object accessed during the cache hit.
    - Return: `None`
    '''
    global m_key_timestamp, _prot_ratio
    _ensure_params(cache_snapshot)
    now = cache_snapshot.access_count
    k = obj.key

    # Maintain a general timestamp ledger and decayed LFU
    m_key_timestamp[k] = now
    s = _decayed_score(k, now) + 1.0
    _set_score(k, s, now)

    if k in _probation:
        # Promote to protected on reuse
        _probation.pop(k, None)
        _protected[k] = now
        # Slightly favor protected upon successful reuse
        delta = 1.0 / max(20, max(int(getattr(cache_snapshot, "capacity", 1)), 1))
        _prot_ratio = _clamp(_prot_ratio + delta, 0.1, 0.9)
    elif k in _protected:
        # Refresh recency within protected
        _protected[k] = now
    else:
        # Metadata miss (should be rare on hit): protect it
        _protected[k] = now

    # Keep protected within target via heat-guided demotion
    _demote_until_quota(cache_snapshot)


def update_after_insert(cache_snapshot, obj):
    '''
    This function defines how the algorithm updates the metadata it maintains immediately after inserting a new object into the cache.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The object that was just inserted into the cache.
    - Return: `None`
    '''
    global m_key_timestamp, _prot_ratio
    _ensure_params(cache_snapshot)
    now = cache_snapshot.access_count
    k = obj.key
    m_key_timestamp[k] = now

    # Ghost-aware admission and ratio adaptation
    g_prot = k in _ghost_protected
    g_prob = k in _ghost_probation
    if g_prot or g_prob:
        if g_prot:
            _ghost_protected.pop(k, None)
            _prot_ratio = _clamp(_prot_ratio + 0.05, 0.1, 0.9)
        if g_prob:
            _ghost_probation.pop(k, None)
            _prot_ratio = _clamp(_prot_ratio - 0.05, 0.1, 0.9)
        # Directly protect ghost hits (ARC-like)
        _protected[k] = now
        # Seed frequency: protected ghosts are stronger than probation ghosts
        base = 1.4 if g_prot and not g_prob else 0.6
        _set_score(k, base, now)
    else:
        # Default cold admission: probation
        _probation[k] = now
        # Admission guard: if last victim was strong, down-seed newcomers
        base = 0.0 if _last_victim_score > 2.0 else 0.05
        _set_score(k, base, now)

    # Enforce protected target by demoting coldest if necessary
    _demote_until_quota(cache_snapshot)

    # Bound ghost histories
    _trim_ghosts(cache_snapshot)


def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    This function defines how the algorithm updates the metadata it maintains immediately after evicting the victim.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The object to be inserted into the cache.
        - `evicted_obj`: The object that was just evicted from the cache.
    - Return: `None`
    '''
    global _last_victim_score
    if evicted_obj is None:
        return
    _ensure_params(cache_snapshot)
    k = evicted_obj.key
    now = cache_snapshot.access_count

    # Compute victim's merit before removal and store for admission guard
    _last_victim_score = _decayed_score(k, now)

    # Move evicted key into the corresponding ghost history
    if k in _probation:
        _probation.pop(k, None)
        _ghost_probation[k] = now
    elif k in _protected:
        _protected.pop(k, None)
        _ghost_protected[k] = now
    else:
        # Unknown to our metadata; assume probation
        _ghost_probation[k] = now

    # Clean general ledger and LFU tables
    m_key_timestamp.pop(k, None)
    _freq.pop(k, None)
    _freq_ts.pop(k, None)

    # Bound ghost histories
    _trim_ghosts(cache_snapshot)

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