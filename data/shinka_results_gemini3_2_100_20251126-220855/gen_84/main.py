# EVOLVE-BLOCK-START
"""Cache eviction algorithm for optimizing hit rates across multiple workloads"""

# S3-FIFO Metadata
# s3_small: FIFO queue for the small segment (probation)
# s3_main: FIFO queue for the main segment (protected)
# s3_ghost: Ghost cache for tracking eviction from small
# s3_freq: Frequency counter for objects (max 3)
s3_small = {}
s3_main = {}
s3_ghost = {}
s3_freq = {}

def evict(cache_snapshot, obj):
    '''
    S3-FIFO Eviction Policy with Extended Ghost and Probabilistic Features:
    - Ghost: Extended to 3x capacity to track history of both Small and Main evictions.
    - Main: Uses gradual demotion (freq-1) to retain popular items longer.
    - Probabilistic Survival: 1% chance for Main items to survive eviction, aiding in loop resistance.
    '''
    global s3_small, s3_main, s3_ghost, s3_freq

    capacity = cache_snapshot.capacity
    s_capacity = max(1, int(capacity * 0.1))

    # Lazy cleanup of ghost - Extended to 3x
    while len(s3_ghost) > 3 * capacity:
        s3_ghost.pop(next(iter(s3_ghost)))

    while True:
        # Decision: Evict from Small or Main?
        if len(s3_small) >= s_capacity or not s3_main:
            if not s3_small:
                return None

            candidate = next(iter(s3_small))
            freq = s3_freq.get(candidate, 0)

            if freq > 0:
                # Promotion: Move to Main
                s3_small.pop(candidate)
                s3_main[candidate] = None
                s3_freq[candidate] = 0
                continue
            else:
                # Victim found in Small
                return candidate

        else:
            # Evict from Main
            candidate = next(iter(s3_main))
            freq = s3_freq.get(candidate, 0)

            if freq > 0:
                # Second Chance: Reinsert to Main tail with demotion
                s3_main.pop(candidate)
                s3_main[candidate] = None
                s3_freq[candidate] = freq - 1
                continue

            # Probabilistic Survival (1% chance)
            if (cache_snapshot.access_count ^ hash(candidate)) % 100 == 0:
                s3_main.pop(candidate)
                s3_main[candidate] = None
                continue

            # Victim found in Main
            return candidate

def update_after_hit(cache_snapshot, obj):
    '''
    S3-FIFO Update on Hit:
    - Increment frequency (cap at 3).
    '''
    global s3_freq
    s3_freq[obj.key] = min(s3_freq.get(obj.key, 0) + 1, 3)

def update_after_insert(cache_snapshot, obj):
    '''
    S3-FIFO Update on Insert:
    - If in Ghost, insert to Main. Else insert to Small.
    '''
    global s3_small, s3_main, s3_ghost, s3_freq
    key = obj.key
    s3_freq[key] = 0

    if key in s3_ghost:
        s3_main[key] = None
        s3_ghost.pop(key)
    else:
        s3_small[key] = None

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    S3-FIFO Update on Evict:
    - Remove from queues.
    - Add to Ghost for BOTH Small and Main evictions to capture full history.
    '''
    global s3_small, s3_main, s3_ghost, s3_freq
    key = evicted_obj.key

    if key in s3_small:
        s3_small.pop(key)
    elif key in s3_main:
        s3_main.pop(key)

    # Add to ghost to track recency/utility beyond cache size
    s3_ghost[key] = None

    if key in s3_freq:
        s3_freq.pop(key)

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