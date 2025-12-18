# EVOLVE-BLOCK-START
"""Cache eviction algorithm for optimizing hit rates across multiple workloads"""

# Adaptive S3-FIFO Globals
as3_small = dict()   # Small queue (FIFO) - Probation
as3_main = dict()    # Main queue (FIFO) - Protected/Frequency
as3_g_small = dict() # Ghost Small (Evicted from Small)
as3_g_main = dict()  # Ghost Main (Evicted from Main)
as3_hits = set()     # Hit bits
as3_p = 0.0          # Adaptive target size for Small queue

def evict(cache_snapshot, obj):
    '''
    Adaptive S3-FIFO Eviction:
    - Uses two queues: Small and Main.
    - Target size of Small is determined by 'as3_p'.
    - Adapts 'as3_p' based on ghost hits (ARC-like logic).
    - Eviction follows S3-FIFO policies (check Small first if oversize, second chance for hits).
    '''
    global as3_small, as3_main, as3_g_small, as3_g_main, as3_hits, as3_p

    capacity = cache_snapshot.capacity

    # Target size for small queue derived from adaptive p
    target_small = int(as3_p)

    while True:
        # Determine eviction candidate source
        # Priority: Small if it exceeds target size or if Main is empty.
        check_small = False
        if len(as3_small) > target_small:
            check_small = True
        elif not as3_main:
            check_small = True

        # Fallback logic if Small is chosen but empty (safety)
        if check_small and not as3_small:
            if as3_main:
                check_small = False
            else:
                # Fallback to any item if both empty
                return next(iter(cache_snapshot.cache))

        if check_small:
            # Candidate from Small
            candidate = next(iter(as3_small))
            if candidate in as3_hits:
                # Hit in Small -> Promote to Main
                as3_hits.discard(candidate)
                del as3_small[candidate]
                as3_main[candidate] = None
            else:
                # Evict from Small
                return candidate
        else:
            # Candidate from Main
            candidate = next(iter(as3_main))
            if candidate in as3_hits:
                # Hit in Main -> Reinsert at tail (Second Chance)
                as3_hits.discard(candidate)
                del as3_main[candidate]
                as3_main[candidate] = None
            else:
                # Evict from Main
                return candidate

def update_after_hit(cache_snapshot, obj):
    '''
    On Cache Hit:
    - Mark object as hit (for second chance / promotion).
    '''
    global as3_hits
    as3_hits.add(obj.key)

def update_after_insert(cache_snapshot, obj):
    '''
    On Cache Insert (Miss):
    - Adapt 'as3_p' if object was in a ghost list.
    - Insert into Small or Main.
    '''
    global as3_small, as3_main, as3_g_small, as3_g_main, as3_hits, as3_p

    # Reset state for new trace
    if cache_snapshot.access_count <= 1:
        as3_small.clear()
        as3_main.clear()
        as3_g_small.clear()
        as3_g_main.clear()
        as3_hits.clear()
        # Initialize p to 10% of capacity
        as3_p = max(1.0, cache_snapshot.capacity * 0.1)

    key = obj.key
    as3_hits.discard(key)

    # Adaptation Logic
    if key in as3_g_small:
        # Hit in Ghost Small -> Small was too small. Increase p.
        delta = 1.0
        lg_s = len(as3_g_small)
        lg_m = len(as3_g_main)
        if lg_s < lg_m and lg_s > 0:
            delta = float(lg_m) / lg_s

        as3_p = min(float(cache_snapshot.capacity), as3_p + delta)

        # Rescue: Remove from ghost and insert into Main
        del as3_g_small[key]
        as3_main[key] = None

    elif key in as3_g_main:
        # Hit in Ghost Main -> Main was too small. Decrease p (shrink Small).
        delta = 1.0
        lg_s = len(as3_g_small)
        lg_m = len(as3_g_main)
        if lg_m < lg_s and lg_m > 0:
            delta = float(lg_s) / lg_m

        as3_p = max(0.0, as3_p - delta)

        # Rescue to Main
        del as3_g_main[key]
        as3_main[key] = None

    else:
        # New item -> Insert into Small
        as3_small[key] = None

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    After Eviction:
    - Move evicted item to corresponding ghost list.
    - Enforce ghost capacity.
    '''
    global as3_small, as3_main, as3_g_small, as3_g_main, as3_hits

    key = evicted_obj.key
    as3_hits.discard(key)

    # Identify source queue and move to ghost
    if key in as3_small:
        del as3_small[key]
        as3_g_small[key] = None
    elif key in as3_main:
        del as3_main[key]
        as3_g_main[key] = None

    # Limit ghost size to capacity
    limit = cache_snapshot.capacity
    while len(as3_g_small) > limit:
        as3_g_small.pop(next(iter(as3_g_small)))
    while len(as3_g_main) > limit:
        as3_g_main.pop(next(iter(as3_g_main)))
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