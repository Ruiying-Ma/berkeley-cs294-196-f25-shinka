# EVOLVE-BLOCK-START
"""Cache eviction algorithm: S3-FIFO with Massive Ghost and Rescue Boost"""

# Global metadata
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
    S3-FIFO with:
    1. Massive Ghost Registry (12x Capacity) to catch large loops.
    2. Frequency Boost on Rescue (Ghost -> Main starts with freq=2).
    3. Probabilistic Survival (1%) to break synchronization.
    '''
    global s3_small, s3_main, s3_ghost, s3_freq

    capacity = cache_snapshot.capacity
    s_capacity = max(1, int(capacity * 0.1))

    # 1. Ghost Management - Keep extensive history
    # 12x capacity allows tracking items in loops much larger than cache
    ghost_limit = int(capacity * 12)
    while len(s3_ghost) > ghost_limit:
        s3_ghost.pop(next(iter(s3_ghost)))

    while True:
        # Decision: Evict from Small or Main?
        # Standard S3-FIFO condition: Evict from Small if it's too big OR Main is empty
        if len(s3_small) >= s_capacity or not s3_main:
            if not s3_small:
                # Should not happen if cache is full and Main is empty
                return None

            candidate = next(iter(s3_small))
            freq = s3_freq.get(candidate, 0)

            if freq > 0:
                # Promotion: Hit in Small -> Move to Main
                s3_small.pop(candidate)
                s3_main[candidate] = None
                s3_freq[candidate] = 0
                continue
            
            # Probabilistic Promotion (1% chance)
            # Helps new items occasionally bypass probation
            if (cache_snapshot.access_count ^ hash(candidate)) % 100 == 0:
                s3_small.pop(candidate)
                s3_main[candidate] = None
                s3_freq[candidate] = 0
                continue

            # Victim found in Small
            return candidate

        else:
            # Evict from Main
            candidate = next(iter(s3_main))
            freq = s3_freq.get(candidate, 0)

            if freq > 0:
                # Reinsertion: Give Second Chance, demote frequency
                # Moves to tail (MRU) of Main FIFO
                s3_main.pop(candidate)
                s3_main[candidate] = None
                s3_freq[candidate] = freq - 1
                continue
            
            # Probabilistic Survival (1% chance)
            # Helps retain some items in Main during cache thrashing
            if (cache_snapshot.access_count ^ hash(candidate)) % 100 == 0:
                s3_main.pop(candidate)
                s3_main[candidate] = None
                continue

            # Victim found in Main
            return candidate

def update_after_hit(cache_snapshot, obj):
    '''
    Update on Hit:
    - Increment frequency (cap at 3).
    '''
    global s3_freq
    s3_freq[obj.key] = min(s3_freq.get(obj.key, 0) + 1, 3)

def update_after_insert(cache_snapshot, obj):
    '''
    Update on Insert:
    - If in Ghost, insert to Main (Recall) with BONUS FREQUENCY.
    - Else insert to Small (New).
    '''
    global s3_small, s3_main, s3_ghost, s3_freq
    key = obj.key
    s3_freq[key] = 0

    if key in s3_ghost:
        s3_main[key] = None
        s3_ghost.pop(key)
        # Rescue Boost: Give it tenure so it survives longer in Main
        # This gives rescued items 2 extra chances in Main (Clock logic)
        s3_freq[key] = 2 
    else:
        s3_small[key] = None

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    Update on Evict:
    - Track evictions in Ghost.
    '''
    global s3_small, s3_main, s3_ghost, s3_freq
    key = evicted_obj.key

    if key in s3_small:
        s3_small.pop(key)
        s3_ghost[key] = None
    elif key in s3_main:
        s3_main.pop(key)
        # Track Main evictions too for maximum history
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