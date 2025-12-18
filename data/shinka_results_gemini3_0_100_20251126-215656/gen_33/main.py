# EVOLVE-BLOCK-START
from collections import OrderedDict

# Advanced S3-FIFO Algorithm
# Implements S3-FIFO with demotion (soft eviction from Main) and ghost queues.
# q_small: FIFO queue for new items (Probation).
# q_main: LRU queue for popular items (Protected).
# q_ghost: FIFO queue for history of evicted items.
# meta_freq: Dictionary tracking access counts for items in cache.

q_small = OrderedDict()
q_main = OrderedDict()
q_ghost = OrderedDict()
meta_freq = {}
last_access_count = -1

def _reset_if_needed(snapshot):
    """Resets state if a new trace is detected."""
    global q_small, q_main, q_ghost, meta_freq, last_access_count
    if snapshot.access_count < last_access_count:
        q_small.clear()
        q_main.clear()
        q_ghost.clear()
        meta_freq.clear()

    # Safety reset if cache is physically empty but we have state
    if not snapshot.cache and (q_small or q_main):
        q_small.clear()
        q_main.clear()
        q_ghost.clear()
        meta_freq.clear()

    last_access_count = snapshot.access_count

def evict(cache_snapshot, obj):
    '''
    Selects a victim using S3-FIFO logic with Main-to-Small demotion.
    '''
    global q_small, q_main, q_ghost, meta_freq
    _reset_if_needed(cache_snapshot)

    capacity = cache_snapshot.capacity
    # Target size for small queue (10% of capacity)
    target_small = max(1, int(capacity * 0.1))

    # We loop to find a victim, processing promotions/demotions along the way.
    # Safety limit to prevent infinite loops (though logic should guarantee termination).
    loop_limit = len(cache_snapshot.cache) * 2
    ops = 0

    while ops < loop_limit:
        ops += 1

        # Decide which queue to evict from
        # If Small is over budget or Main is empty, we must evict from Small.
        # Otherwise, we evict from Main.
        evict_from_small = False
        if len(q_small) > target_small or len(q_main) == 0:
            evict_from_small = True

        if evict_from_small:
            if not q_small:
                # Should only happen if Main is empty too (cache empty?)
                if q_main:
                    # Fallback to Main
                    candidate, _ = q_main.popitem(last=False)
                    q_ghost[candidate] = None
                    meta_freq.pop(candidate, None)
                    return candidate
                # Absolute fallback
                return next(iter(cache_snapshot.cache))

            # FIFO eviction from Small
            candidate, _ = q_small.popitem(last=False)

            # Check for promotion: if accessed while in Small
            freq = meta_freq.get(candidate, 0)
            if freq > 0:
                # Promote to Main
                q_main[candidate] = None
                meta_freq[candidate] = 0 # Reset frequency cost
                continue # Retry eviction
            else:
                # Evict candidate
                q_ghost[candidate] = None
                if len(q_ghost) > capacity:
                    q_ghost.popitem(last=False)
                meta_freq.pop(candidate, None)
                return candidate

        else:
            # Evict from Main (LRU)
            candidate, _ = q_main.popitem(last=False)

            # Check for demotion: if accessed while in Main (freq > 0)
            # This gives "warm" items a second chance in Small
            freq = meta_freq.get(candidate, 0)
            if freq > 0:
                # Demote to Small
                q_small[candidate] = None
                meta_freq[candidate] = 0 # Reset frequency
                continue # Retry eviction
            else:
                # Evict candidate
                q_ghost[candidate] = None
                if len(q_ghost) > capacity:
                    q_ghost.popitem(last=False)
                meta_freq.pop(candidate, None)
                return candidate

    # Fallback if loop limit reached
    if q_small:
        k, _ = q_small.popitem(last=False)
        return k
    return next(iter(cache_snapshot.cache))

def update_after_hit(cache_snapshot, obj):
    '''
    On hit: increment frequency, maintain LRU order for Main.
    '''
    global q_small, q_main, meta_freq
    _reset_if_needed(cache_snapshot)

    key = obj.key
    meta_freq[key] = meta_freq.get(key, 0) + 1

    if key in q_main:
        q_main.move_to_end(key)
    # If in Small, do not move (lazy promotion)

def update_after_insert(cache_snapshot, obj):
    '''
    On insert: Handle Ghost hits or new insertions.
    '''
    global q_small, q_main, q_ghost, meta_freq
    _reset_if_needed(cache_snapshot)

    key = obj.key
    # Start with 0 frequency
    meta_freq[key] = 0

    if key in q_ghost:
        # Ghost Hit: Restore to Main
        del q_ghost[key]
        q_main[key] = None
    else:
        # New Item: Insert to Small
        q_small[key] = None

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    Cleanup internal state.
    '''
    global q_small, q_main, meta_freq
    _reset_if_needed(cache_snapshot)

    key = evicted_obj.key
    # Ensure removal
    q_small.pop(key, None)
    q_main.pop(key, None)
    meta_freq.pop(key, None)
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