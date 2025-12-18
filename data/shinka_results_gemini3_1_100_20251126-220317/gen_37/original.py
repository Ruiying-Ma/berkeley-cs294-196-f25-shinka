# EVOLVE-BLOCK-START
"""Cache eviction algorithm for optimizing hit rates across multiple workloads"""

# S3-FIFO-Refined Globals
q_small = dict()
q_main = dict()
q_ghost = dict()
s_hits = set()

def evict(cache_snapshot, obj):
    '''
    S3-FIFO Eviction Logic:
    - 10% Small (Nursery), 90% Main.
    - Lazy promotion/reinsertion on eviction.
    '''
    global q_small, q_main, q_ghost, s_hits

    capacity = cache_snapshot.capacity
    # Target size for small queue
    target_small = max(1, int(capacity * 0.1))

    while True:
        # Check Small first if it's over budget or Main is empty
        if len(q_small) > target_small or not q_main:
            if not q_small:
                # Fallback if both empty (should not happen in full cache)
                if q_main:
                    candidate = next(iter(q_main))
                else:
                    return next(iter(cache_snapshot.cache))
            else:
                candidate = next(iter(q_small))

            if candidate in s_hits:
                # Hit in Small -> Promote to Main
                s_hits.discard(candidate)
                if candidate in q_small:
                    del q_small[candidate]
                q_main[candidate] = None
            else:
                # Evict from Small
                return candidate
        else:
            # Check Main
            if not q_main:
                # Should not reach here due to loop logic, but safety
                target_small = -1 # Force small check next loop
                continue

            candidate = next(iter(q_main))
            if candidate in s_hits:
                # Hit in Main -> Reinsert to Main Tail
                s_hits.discard(candidate)
                if candidate in q_main:
                    del q_main[candidate]
                q_main[candidate] = None
            else:
                # Evict from Main
                return candidate

def update_after_hit(cache_snapshot, obj):
    '''
    Record hit.
    '''
    global s_hits
    s_hits.add(obj.key)

def update_after_insert(cache_snapshot, obj):
    '''
    Handle new insertion:
    - Reset state if trace changed.
    - Insert to Main if Ghost, else Small.
    '''
    global q_small, q_main, q_ghost, s_hits

    # Detect trace reset
    if cache_snapshot.access_count <= 1:
        q_small.clear()
        q_main.clear()
        q_ghost.clear()
        s_hits.clear()

    key = obj.key
    # Clear hit status for the new/re-inserted object
    s_hits.discard(key)

    if key in q_ghost:
        # Rescue: Ghost -> Main
        del q_ghost[key]
        q_main[key] = None
    else:
        # New -> Small
        q_small[key] = None

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    Cleanup and Ghost management.
    '''
    global q_small, q_main, q_ghost, s_hits

    key = evicted_obj.key

    # Remove from queues if present (victim chosen in evict is not removed there)
    if key in q_small:
        del q_small[key]
        # Evicted from Small -> Ghost
        q_ghost[key] = None
    elif key in q_main:
        del q_main[key]
        # Evicted from Main -> No Ghost (standard S3-FIFO)

    if key in s_hits:
        s_hits.discard(key)

    # Manage Ghost capacity (same as cache capacity)
    while len(q_ghost) > cache_snapshot.capacity:
        q_ghost.pop(next(iter(q_ghost)))

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