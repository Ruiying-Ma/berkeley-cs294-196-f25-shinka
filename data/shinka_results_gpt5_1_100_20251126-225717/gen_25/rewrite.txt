# EVOLVE-BLOCK-START
"""CLOCK-Pro–inspired adaptive cache with scan detection and ghost feedback.

Core ideas:
- Maintain a single circular order (clock) of resident keys with reference bits.
- Pages are hot or cold; insert cold with ref=1. On hit, set ref=1 and (optionally) promote.
- Eviction sweeps the clock: clear ref, demote hot->cold when ref=0, evict first cold with ref=0.
- A bounded ghost history tracks recently evicted keys to bias hot target and promotion.
- A lightweight scan detector temporarily shrinks hot target and tightens promotion.
"""

from collections import OrderedDict

# Fallback ledger retained for compatibility with prior frameworks
m_key_timestamp = dict()

# CLOCK state
_clock = OrderedDict()   # key -> None (order represents clock order; head is eviction hand)
_ref = dict()            # key -> 0/1 reference bit
_hot = set()             # keys currently classified as hot
_hot_target = None       # desired number of hot pages
_last_capacity = None    # last seen capacity

# Ghost history: key -> last timestamp seen/evicted
_ghost = OrderedDict()

# Scan detection window
_win_access = 0
_win_hits = 0
_win_inserts = 0
_scan_until = 0

# Touch counter to require second touch before hot-promotion (used especially under scans)
_touches = dict()        # key -> touches since insert/promotion


def _cap(cache_snapshot):
    cap = getattr(cache_snapshot, "capacity", None)
    if isinstance(cap, int) and cap > 0:
        return cap
    # Fallback: number of objects currently resident
    return max(1, len(cache_snapshot.cache))


def _init_if_needed(cache_snapshot):
    global _hot_target, _last_capacity
    cap = _cap(cache_snapshot)
    if _hot_target is None or _last_capacity != cap:
        # Start with modest hot share; adapt via ghosts and scan detector
        _hot_target = max(1, int(0.3 * cap))
        _last_capacity = cap


def _sync_clock_with_cache(cache_snapshot):
    """Keep clock membership consistent with actual cache."""
    in_cache = set(cache_snapshot.cache.keys())
    # Remove keys no longer in cache
    for k in list(_clock.keys()):
        if k not in in_cache:
            _clock.pop(k, None)
            _ref.pop(k, None)
            _hot.discard(k)
            _touches.pop(k, None)
    # Seed any missing cached keys conservatively as cold with ref=0 (rare path)
    for k in in_cache:
        if k not in _clock:
            _clock[k] = None
            _ref[k] = 0
            _hot.discard(k)
            _touches[k] = 0


def _clamp_hot_target(cache_snapshot):
    global _hot_target
    cap = _cap(cache_snapshot)
    if _hot_target < 1:
        _hot_target = 1
    if _hot_target > cap:
        _hot_target = cap


def _enforce_hot_target(cache_snapshot):
    """Demote oldest hot keys until hot_count <= hot_target."""
    _clamp_hot_target(cache_snapshot)
    cap = _cap(cache_snapshot)
    if not _clock or not _hot:
        return
    safety_steps = 0
    # Demote using a small sweep to find hot with ref=0; clear ref if needed
    while len(_hot) > _hot_target and safety_steps <= 2 * max(1, len(_clock)):
        safety_steps += 1
        k = next(iter(_clock))
        if k in _hot and _ref.get(k, 0) == 0:
            _hot.discard(k)        # demote to cold
            _clock.move_to_end(k)  # advance hand
        else:
            # clear ref and move along
            _ref[k] = 0
            _clock.move_to_end(k)
    # If still over target (all hot recently referenced), force demotions of oldest hots
    while len(_hot) > _hot_target:
        # demote the oldest hot we can find
        for k in list(_clock.keys()):
            if k in _hot:
                _hot.discard(k)
                break
        else:
            break  # no hot found (shouldn't happen)


def _record_window(cache_snapshot, is_hit=False, is_insert=False):
    """Update sliding window stats and set scan mode when a scan is detected."""
    global _win_access, _win_hits, _win_inserts, _scan_until
    cap = _cap(cache_snapshot)
    W = max(32, 2 * cap)
    _win_access += 1
    if is_hit:
        _win_hits += 1
    if is_insert:
        _win_inserts += 1
    if _win_access >= W:
        hit_rate = _win_hits / float(_win_access)
        insert_rate = _win_inserts / float(_win_access)
        if insert_rate > 0.6 and hit_rate < 0.2:
            # Enter scan mode for next window
            _scan_until = cache_snapshot.access_count + W
        # reset window
        _win_access = 0
        _win_hits = 0
        _win_inserts = 0


def _in_scan_mode(cache_snapshot):
    return cache_snapshot.access_count < _scan_until


def _ghost_trim(cache_snapshot):
    """Bound ghost history to ~capacity; drop oldest first."""
    cap = _cap(cache_snapshot)
    max_ghost = max(cap, 128)
    # pop oldest entries beyond bound
    while len(_ghost) > max_ghost:
        _ghost.popitem(last=False)


def _promote_on_hit(cache_snapshot, key, now):
    """Handle promotion policy on hit."""
    # If already hot, just set ref and return
    if key in _hot:
        _ref[key] = 1
        return
    # Decide promotion rule
    if _in_scan_mode(cache_snapshot):
        # Require two touches under scans
        _touches[key] = _touches.get(key, 0) + 1
        if _touches[key] >= 2:
            _hot.add(key)
            _touches[key] = 0
    else:
        # Promote on first hit
        _hot.add(key)
        _touches[key] = 0
    _ref[key] = 1
    _enforce_hot_target(cache_snapshot)


def evict(cache_snapshot, obj):
    """
    Choose victim using CLOCK-Pro style sweep:
    - Clear ref=1 to 0 and move node to MRU.
    - Demote hot(ref=0) -> cold and continue.
    - Evict first cold(ref=0) encountered.
    """
    _init_if_needed(cache_snapshot)
    _sync_clock_with_cache(cache_snapshot)
    if not _clock:
        # Fallback to any key in cache
        for k in cache_snapshot.cache:
            return k
        return None

    steps = 0
    limit = max(1, 2 * len(_clock))
    while steps < limit and _clock:
        steps += 1
        k = next(iter(_clock))
        r = _ref.get(k, 0)
        if r == 1:
            _ref[k] = 0
            _clock.move_to_end(k)
            continue
        # r == 0
        if k in _hot:
            # demote hot to cold, advance
            _hot.discard(k)
            _clock.move_to_end(k)
            continue
        else:
            # cold and unreferenced -> victim
            return k

    # If sweep failed (unlikely), fallback to oldest (LRU head)
    for k in _clock:
        return k
    # ultimate fallback
    for k in cache_snapshot.cache:
        return k
    return None


def update_after_hit(cache_snapshot, obj):
    """Set ref bit, possibly promote, and update scan/window/ghost trims."""
    global m_key_timestamp
    _init_if_needed(cache_snapshot)
    _sync_clock_with_cache(cache_snapshot)
    now = cache_snapshot.access_count
    k = obj.key

    # Ensure in clock
    if k not in _clock:
        _clock[k] = None
        _ref[k] = 0
        _touches[k] = 0

    # Hit handling
    _promote_on_hit(cache_snapshot, k, now)
    # Move to MRU to simulate advancing hand past recently used
    _clock.move_to_end(k)
    m_key_timestamp[k] = now

    # Update sliding window and scan state
    _record_window(cache_snapshot, is_hit=True, is_insert=False)

    # Keep ghost bounded
    _ghost_trim(cache_snapshot)


def update_after_insert(cache_snapshot, obj):
    """Insert as cold(ref=1). If recently evicted (ghost), bump hot_target and promote."""
    global m_key_timestamp, _hot_target
    _init_if_needed(cache_snapshot)
    _sync_clock_with_cache(cache_snapshot)
    now = cache_snapshot.access_count
    k = obj.key
    cap = _cap(cache_snapshot)

    # Sliding-window update for scan detection
    _record_window(cache_snapshot, is_hit=False, is_insert=True)

    # Insert (or refresh placement) at MRU
    if k in _clock:
        # If already present in metadata (rare), keep state but set ref
        _ref[k] = 1
        _clock.move_to_end(k)
    else:
        _clock[k] = None
        _ref[k] = 1
        _touches[k] = 0

    # Ghost-guided admission/promotion and target tuning
    if k in _ghost:
        # Recently evicted -> increase hot target slightly and promote to hot
        _hot_target = min(cap, _hot_target + 1)
        _hot.add(k)
        _ghost.pop(k, None)
    else:
        # Under scans, keep hot target small to avoid pollution
        if _in_scan_mode(cache_snapshot):
            _hot_target = max(1, cap // 10)
        # Do not auto-promote; will promote on subsequent hit
        _hot.discard(k)

    # Move to MRU to simulate hand moving past new page
    _clock.move_to_end(k)
    m_key_timestamp[k] = now

    # Enforce current hot target
    _enforce_hot_target(cache_snapshot)

    # Keep ghost bounded
    _ghost_trim(cache_snapshot)


def update_after_evict(cache_snapshot, obj, evicted_obj):
    """Record victim in ghost, remove from clock, and adapt hot target if needed."""
    if evicted_obj is None:
        return
    _init_if_needed(cache_snapshot)
    ek = evicted_obj.key
    now = cache_snapshot.access_count

    # Add to ghost (MRU); if exists, refresh position
    if ek in _ghost:
        _ghost.pop(ek, None)
    _ghost[ek] = now

    # Remove from clock metadata
    if ek in _clock:
        _clock.pop(ek, None)
    _ref.pop(ek, None)
    _hot.discard(ek)
    _touches.pop(ek, None)
    m_key_timestamp.pop(ek, None)

    # If we ever evicted a hot page (should be rare), shrink hot target a bit
    # This indicates hot set pressure was too high.
    global _hot_target
    if ek in _hot:
        _hot_target = max(1, _hot_target - 1)

    # Trim ghost to bounded size
    _ghost_trim(cache_snapshot)
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