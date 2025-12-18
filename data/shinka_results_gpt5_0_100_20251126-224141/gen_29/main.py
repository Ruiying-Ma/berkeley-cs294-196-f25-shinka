# EVOLVE-BLOCK-START
"""Cache eviction algorithm for optimizing hit rates across multiple workloads
LRU-K (K=2) with Correlated Reference Period (CRP)
- Maintain the last two distinct reference times per key across evictions.
- Evict the item with the largest backward K-distance (or cold items with no 2nd ref).
- CRP avoids counting rapid, correlated hits as separate references.
This filters one-hit wonders and favors items with proven reuse.
"""

# Per-key compact history: key -> (last_ts, prev_ts)
# last_ts: timestamp of most recent reference
# prev_ts: timestamp of the second most recent (distinct) reference, or None if not available
m_history = dict()


def _cap(cache_snapshot):
    try:
        return max(1, int(cache_snapshot.capacity))
    except Exception:
        # Fallback to count-based capacity
        return max(1, len(cache_snapshot.cache))


def _crp_window(cache_snapshot):
    # Correlated reference period window. Hits closer than this window are not counted as distinct.
    cap = _cap(cache_snapshot)
    return max(1, cap // 16)


def _update_history_for_ref(now, key, crp):
    """Record a reference to key at time 'now', honoring CRP."""
    last, prev = m_history.get(key, (None, None))
    if last is None:
        # First-ever reference
        m_history[key] = (now, None)
        return

    # If the last reference is within CRP, treat this as the same burst: refresh last only.
    if now - last < crp:
        m_history[key] = (now, prev)
    else:
        # Distinct reference: shift last to prev and set new last
        m_history[key] = (now, last)


def _prune_history(cache_snapshot):
    """Bound history size by trimming oldest non-resident entries."""
    cap = _cap(cache_snapshot)
    # Allow history up to 16x capacity; prune conservatively beyond that.
    limit = max(16 * cap, 64)
    if len(m_history) <= limit:
        return
    resident = set(cache_snapshot.cache.keys())
    # Build a list of non-resident entries with their last_ts (older first)
    victims = []
    for k, (last, prev) in m_history.items():
        if k not in resident:
            # Treat missing timestamps as very old
            victims.append((last if last is not None else -1, k))
    # Remove up to the overflow amount from the oldest non-resident histories
    overflow = len(m_history) - limit
    if overflow <= 0 or not victims:
        return
    victims.sort(key=lambda x: x[0])  # oldest first
    for i in range(min(overflow, len(victims))):
        _, k = victims[i]
        m_history.pop(k, None)


def evict(cache_snapshot, obj):
    '''
    Choose eviction victim using LRU-2 with CRP:
    - Prefer keys without a second distinct reference (cold).
    - Otherwise, pick the one with the largest backward 2nd-reference distance.
    - Tie-break by older last reference.
    '''
    now = cache_snapshot.access_count
    cache_keys = cache_snapshot.cache.keys()
    if not cache_keys:
        return None

    # Select victim by maximizing the tuple: (is_cold, k_distance, last_age)
    # where:
    #   is_cold = 1 if prev_ts is None else 0
    #   k_distance = inf if prev_ts is None else now - prev_ts
    #   last_age = now - last_ts
    best_key = None
    best_tuple = (float('-inf'), float('-inf'), float('-inf'))

    for k in cache_keys:
        last, prev = m_history.get(k, (None, None))
        # Compute components
        is_cold = 1 if prev is None else 0
        if prev is None:
            k_distance = float('inf')
        else:
            k_distance = now - prev
        if last is None:
            last_age = float('inf')
        else:
            last_age = now - last

        candidate = (is_cold, k_distance, last_age)
        if candidate > best_tuple:
            best_tuple = candidate
            best_key = k

    # Fallback (should not trigger, but keep for robustness)
    if best_key is None:
        best_key = next(iter(cache_snapshot.cache.keys()))
    return best_key


def update_after_hit(cache_snapshot, obj):
    '''
    On hit, record a reference with CRP handling.
    '''
    now = cache_snapshot.access_count
    crp = _crp_window(cache_snapshot)
    _update_history_for_ref(now, obj.key, crp)
    _prune_history(cache_snapshot)


def update_after_insert(cache_snapshot, obj):
    '''
    After insert (which follows a miss), count it as a reference.
    This links consecutive misses to a key into the LRU-2 history across evictions.
    '''
    now = cache_snapshot.access_count
    crp = _crp_window(cache_snapshot)
    _update_history_for_ref(now, obj.key, crp)
    _prune_history(cache_snapshot)


def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    We keep histories across evictions to recognize returning items.
    Only prune history opportunistically to bound memory.
    '''
    _prune_history(cache_snapshot)

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