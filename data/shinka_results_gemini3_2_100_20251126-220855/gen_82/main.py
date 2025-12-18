# EVOLVE-BLOCK-START
"""Cache eviction algorithm: S3-FIFO with Probabilistic Survival and Extended Ghost"""

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
    Randomized S3-FIFO Eviction Policy:
    - Structure: Small (10%) + Main (90%) + Ghost (300%).
    - Key Innovation: Deterministic Survival + Persistent History.
      - Extended Ghost (3x) tracks both Small and Main evictions.
      - Deterministic Survival (5%): Ensures a subset of keys survives large loops.
    '''
    global s3_small, s3_main, s3_ghost, s3_freq

    capacity = cache_snapshot.capacity
    s_capacity = max(1, int(capacity * 0.1))

    # Lazy cleanup of ghost - Extended to 3x capacity
    while len(s3_ghost) > 3 * capacity:
        victim = next(iter(s3_ghost))
        s3_ghost.pop(victim)
        if victim in s3_freq:
            del s3_freq[victim]

    while True:
        # Decision: Evict from Small or Main?
        # If Small is larger than target, evict from Small.
        # Also if Main is empty, we must evict from Small.
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

            # Probabilistic (1%) OR Deterministic (5%) Promotion
            # Deterministic check ensures specific items always bypass probation,
            # creating a static resident set even in adverse patterns.
            h_val = hash(candidate)
            if (cache_snapshot.access_count ^ h_val) % 100 == 0 or h_val % 20 == 0:
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
                s3_main.pop(candidate)
                s3_main[candidate] = None
                s3_freq[candidate] = freq - 1
                continue

            # Probabilistic (1%) OR Deterministic (5%) Survival
            # Allows 5% of items to persist in Main regardless of frequency.
            # Solves "Loop > Cache" failure by pinning a subset of data.
            h_val = hash(candidate)
            if (cache_snapshot.access_count ^ h_val) % 100 == 0 or h_val % 20 == 0:
                s3_main.pop(candidate)
                s3_main[candidate] = None
                # No freq boost, just survival.
                continue

            # Victim found in Main
            return candidate

def update_after_hit(cache_snapshot, obj):
    '''
    Update on Hit:
    - Increment frequency (cap at 3).
    - Do NOT move to MRU in Main (Keep FIFO for scan resistance).
    '''
    global s3_freq
    s3_freq[obj.key] = min(s3_freq.get(obj.key, 0) + 1, 3)

def update_after_insert(cache_snapshot, obj):
    '''
    Update on Insert:
    - If in Ghost, insert to Main (Recall) and restore decayed freq.
    - Else insert to Small (New) and reset freq.
    '''
    global s3_small, s3_main, s3_ghost, s3_freq
    key = obj.key

    if key in s3_ghost:
        s3_main[key] = None
        s3_ghost.pop(key)
        # Restore frequency with decay (halved)
        # Helps formerly hot items survive in Main
        s3_freq[key] = s3_freq.get(key, 0) // 2
    else:
        s3_small[key] = None
        s3_freq[key] = 0

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    Update on Evict:
    - Track all evictions in Ghost (Small and Main).
    - Preserve frequency data for Ghost items.
    '''
    global s3_small, s3_main, s3_ghost, s3_freq
    key = evicted_obj.key

    if key in s3_small:
        s3_small.pop(key)
        s3_ghost[key] = None
    elif key in s3_main:
        s3_main.pop(key)
        # Also track Main evictions to capture large loops
        s3_ghost[key] = None

    # Do NOT pop s3_freq if in ghost. Cleaned up in evict().
    if key not in s3_ghost and key in s3_freq:
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