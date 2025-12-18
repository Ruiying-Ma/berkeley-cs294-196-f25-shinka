# EVOLVE-BLOCK-START
"""Cache eviction algorithm using decayed LFU with ghost-admission bias."""

import math

# Compatibility ledger from legacy code (kept but not relied upon for policy)
m_key_timestamp = dict()

# Per-key decayed frequency state
_score = dict()          # key -> decayed frequency score (float)
_last_update = dict()    # key -> last timestamp when score was updated (int)

# Ghost history: tracks last-access time for recently evicted keys to bias re-admission
_ghost_last_access = dict()  # key -> last access time recorded at eviction

# Segment-aware ghost origins for ARC-like feedback
_ghost_from_prob = dict()  # key -> last timestamp (evicted from probation)
_ghost_from_prot = dict()  # key -> last timestamp (evicted from protected)

# SLRU segments
_probation = set()
_protected = set()

# Adaptive protected ratio with momentum
_prot_ratio = 0.8
_PROT_MIN = 0.05
_PROT_MAX = 0.90
_prot_momentum = 0.0

# Lightweight scan detector (sliding window)
_scan_mode = False
_win_start = 0
_win_window = 0
_win_hits = 0
_win_misses = 0
_win_seen = set()
_win_new_inserts = 0


def _half_life(cache_snapshot):
    """
    Choose a decay half-life scaled to cache capacity.
    Larger caches get a longer memory; smaller caches emphasize recency more.
    """
    cap = max(int(getattr(cache_snapshot, "capacity", 1)), 1)
    # Empirically effective: ~1.5x capacity accesses as half-life
    return max(8, int(1.5 * cap))


def _decay_factor(delta, hl):
    if hl <= 0:
        return 0.0
    # Exponential decay to half every 'hl' accesses: 0.5 ** (delta/hl)
    return math.pow(0.5, float(delta) / float(hl))


def _current_score(key, now, hl):
    s = _score.get(key, 0.0)
    last = _last_update.get(key, now)
    if s <= 0.0:
        return 0.0
    delta = now - last
    if delta <= 0:
        return s
    return s * _decay_factor(delta, hl)


def _protected_limit(cache_snapshot):
    cap = max(int(getattr(cache_snapshot, "capacity", 1)), 1)
    # Adaptive protected sizing; in scan mode, cap protected more aggressively.
    ratio = _prot_ratio
    if _scan_mode:
        ratio = min(ratio, 0.20)
    return max(1, int(ratio * cap))


def _prune_and_seed_segments(cache_snapshot):
    """Keep SLRU segments consistent with actual cache keys and seed unknown keys into probation."""
    in_cache = set(cache_snapshot.cache.keys())
    # Drop any keys that are no longer in cache
    _probation.intersection_update(in_cache)
    _protected.intersection_update(in_cache)
    # Seed any missing cached keys into probation (e.g., after cold start)
    unknown = in_cache.difference(_probation).difference(_protected)
    if unknown:
        _probation.update(unknown)


def _window_tick(cache_snapshot, is_hit=False, is_insert=False, key=None):
    """Maintain a sliding window to detect scans."""
    global _win_start, _win_window, _win_hits, _win_misses, _win_seen, _win_new_inserts, _scan_mode
    now = cache_snapshot.access_count
    cap = max(int(getattr(cache_snapshot, "capacity", 1)), 1)
    if _win_window <= 0:
        _win_window = max(16, 2 * cap)
        _win_start = now
        _win_hits = 0
        _win_misses = 0
        _win_seen = set()
        _win_new_inserts = 0

    if is_hit:
        _win_hits += 1
    if is_insert:
        _win_misses += 1

    if key is not None:
        first_time = key not in _win_seen
        if first_time:
            _win_seen.add(key)
        if is_insert and first_time:
            _win_new_inserts += 1

    # End of window -> evaluate scan conditions
    if now - _win_start >= _win_window:
        total = _win_hits + _win_misses
        hit_rate = (_win_hits / total) if total > 0 else 0.0
        unique_insert_rate = float(_win_new_inserts) / float(_win_window)
        # Enter scan mode when many unique inserts and low hit rate
        _scan_mode = (unique_insert_rate > 0.6 and hit_rate < 0.2)

        # Reset window
        _win_start = now
        _win_window = max(16, 2 * cap)
        _win_hits = 0
        _win_misses = 0
        _win_seen.clear()
        _win_new_inserts = 0


def _trim_ghost_dicts(cache_snapshot):
    """Bound ghost dict sizes to avoid unbounded growth."""
    cap = max(int(getattr(cache_snapshot, "capacity", 1)), 1)
    max_ghost = max(50 * cap, 100)
    for d in (_ghost_last_access, _ghost_from_prob, _ghost_from_prot):
        if len(d) > max_ghost:
            target = len(d) - max_ghost
            if target <= 0:
                continue
            stale_items = sorted(d.items(), key=lambda x: x[1])[:target]
            for key, _ in stale_items:
                d.pop(key, None)


def _adjust_prot_ratio(cache_snapshot, delta_ratio):
    """Adjust protected ratio with momentum and bounds."""
    global _prot_ratio, _prot_momentum
    # Smooth adjustments to avoid oscillations
    _prot_momentum = 0.5 * _prot_momentum + float(delta_ratio)
    _prot_ratio = max(_PROT_MIN, min(_PROT_MAX, _prot_ratio + _prot_momentum))


def _enforce_protected_limit(cache_snapshot):
    """Demote oldest from protected until within limit."""
    limit = _protected_limit(cache_snapshot)
    if not _protected:
        return
    while len(_protected) > limit:
        # Demote LRU from protected to probation
        oldest = None
        oldest_ts = None
        for kk in _protected:
            ts = _last_update.get(kk, 0)
            if oldest_ts is None or ts < oldest_ts:
                oldest = kk
                oldest_ts = ts
        if oldest is None:
            break
        _protected.discard(oldest)
        _probation.add(oldest)


def evict(cache_snapshot, obj):
    '''
    This function defines how the algorithm chooses the eviction victim.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The new object that needs to be inserted into the cache.
    - Return:
        - `candid_obj_key`: The key of the cached object that will be evicted to make room for `obj`.
    '''
    now = cache_snapshot.access_count
    hl = _half_life(cache_snapshot)

    # Ensure segments reflect current cache contents
    _prune_and_seed_segments(cache_snapshot)

    in_cache = set(cache_snapshot.cache.keys())
    prob = _probation.intersection(in_cache)
    prot = _protected.intersection(in_cache)

    # Prefer evicting from probation (SLRU). Use LRU with frequency tiebreaker.
    if prob:
        victim = None
        best_tuple = None  # (last_update, decayed_score)
        for k in prob:
            lu = _last_update.get(k, 0)
            cs = _current_score(k, now, hl)
            tup = (lu, cs)
            if best_tuple is None or tup < best_tuple:
                best_tuple = tup
                victim = k
        return victim

    # If probation empty, evict from protected: pick lowest decayed score, tiebreak by oldest
    if prot:
        victim = None
        min_score = None
        min_last = None
        for k in prot:
            cs = _current_score(k, now, hl)
            lu = _last_update.get(k, 0)
            if (min_score is None) or (cs < min_score) or (cs == min_score and lu < min_last):
                victim = k
                min_score = cs
                min_last = lu
        return victim

    # Fallback (should not happen): pick any key in cache
    for k in cache_snapshot.cache:
        return k
    return None


def update_after_hit(cache_snapshot, obj):
    '''
    Update metadata immediately after a cache hit.
    '''
    global m_key_timestamp, _score, _last_update, _ghost_last_access, _probation, _protected

    now = cache_snapshot.access_count
    k = obj.key
    hl = _half_life(cache_snapshot)

    # Window accounting for scan detection
    _window_tick(cache_snapshot, is_hit=True, key=k)

    # Ensure segments reflect current cache contents
    _prune_and_seed_segments(cache_snapshot)

    # Update decayed score lazily on access
    cur = _current_score(k, now, hl)
    _score[k] = cur + 1.0
    _last_update[k] = now

    # Promotion: if hit in probation, move to protected, but be stricter during scans
    if k in _probation:
        promote = True
        if _scan_mode:
            # Require an extra touch before promotion during scans
            promote = (cur >= 0.9)
        if promote:
            _probation.discard(k)
            _protected.add(k)
    elif k not in _protected:
        # If key was unknown to segments but in cache, start then promote
        if not _scan_mode:
            _protected.add(k)
        else:
            # During scans, avoid immediate promotion of unknowns
            _probation.add(k)

    # Enforce protected size limit via demotion (LRU)
    _enforce_protected_limit(cache_snapshot)

    # Maintain general ledger and ghost recency (used for admission)
    m_key_timestamp[k] = now
    _ghost_last_access[k] = now  # keep last access fresh for this key


def update_after_insert(cache_snapshot, obj):
    '''
    Update metadata immediately after inserting a new object into the cache.
    '''
    global m_key_timestamp, _score, _last_update, _ghost_last_access, _probation, _protected

    now = cache_snapshot.access_count
    k = obj.key
    hl = _half_life(cache_snapshot)

    # Window accounting for scan detection
    _window_tick(cache_snapshot, is_insert=True, key=k)

    # Admission bias: segment-aware ghosts and scan handling
    base = 0.25
    place_in_protected = False

    # Recent ghost recency
    last_ghost_any = _ghost_last_access.get(k)
    recent_any = (last_ghost_any is not None and (now - last_ghost_any) <= hl)

    last_prob = _ghost_from_prob.get(k)
    last_prot = _ghost_from_prot.get(k)
    recent_prob = (last_prob is not None and (now - last_prob) <= hl)
    recent_prot = (last_prot is not None and (now - last_prot) <= hl)

    # ARC-like protected sizing adaptation with momentum
    step = 0.03
    if recent_prob:
        _adjust_prot_ratio(cache_snapshot, -step)  # favor recency -> shrink protected
        _ghost_from_prob.pop(k, None)
    elif recent_prot:
        _adjust_prot_ratio(cache_snapshot, +step)  # favor frequency -> grow protected
        _ghost_from_prot.pop(k, None)

    # Admission placement
    if recent_any and recent_prot and not _scan_mode:
        base = 1.2
        place_in_protected = True
    elif _scan_mode:
        # In scans, never admit directly to protected
        place_in_protected = False
        base = 0.2
    else:
        # Default conservative probation admission
        place_in_protected = False
        base = 0.25

    _score[k] = base
    _last_update[k] = now
    m_key_timestamp[k] = now

    # Place new object into SLRU segments
    _prune_and_seed_segments(cache_snapshot)
    if place_in_protected:
        _protected.add(k)
        _probation.discard(k)
        _enforce_protected_limit(cache_snapshot)
    else:
        _probation.add(k)
        _protected.discard(k)
    # Keep ghost record; don't delete so brief reinsertions can still get bias.


def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    Update metadata immediately after evicting the victim.
    '''
    global m_key_timestamp, _score, _last_update, _ghost_last_access, _probation, _protected, _ghost_from_prob, _ghost_from_prot

    if evicted_obj is None:
        return

    ek = evicted_obj.key
    now = cache_snapshot.access_count

    # Determine origin segment before removal
    was_protected = ek in _protected
    was_probation = ek in _probation

    # Record last access time of evicted key into ghost for admission decisions
    last_seen = _last_update.get(ek, now)
    _ghost_last_access[ek] = last_seen
    if was_protected:
        _ghost_from_prot[ek] = now
    else:
        # Default to probation ghost if unknown
        _ghost_from_prob[ek] = now

    # Remove from main metadata and SLRU segments
    _score.pop(ek, None)
    _last_update.pop(ek, None)
    m_key_timestamp.pop(ek, None)
    _probation.discard(ek)
    _protected.discard(ek)

    # Bound ghost sizes across all ghost dicts
    _trim_ghost_dicts(cache_snapshot)

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