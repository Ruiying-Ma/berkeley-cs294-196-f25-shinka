# EVOLVE-BLOCK-START
"""Cache eviction: S3-FIFO with 5% Downsampling Promotion for Loop/Scan Resistance"""

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
    S3-FIFO with Sampling:
    - Small (10%) / Main (90%).
    - Ghost (3x Capacity).
    - Main Eviction: Second Chance (Freq > 0 -> Reinsert & Decr).
    - Small Eviction:
      - If Freq > 0 (Hit in Small): Promote to Main.
      - If Random(5%): Promote to Main (Downsampling).
        - This allows a subset of items from large loops (>Cache) to populate Main
          and establish a working set, converting 0% hit rate to ~5%.
      - Else: Evict to Ghost.
    '''
    global s3_small, s3_main, s3_ghost, s3_freq

    capacity = cache_snapshot.capacity
    s_capacity = max(1, int(capacity * 0.1))

    # Lazy cleanup of ghost - Keep generous history (3x)
    while len(s3_ghost) > 3 * capacity:
        s3_ghost.pop(next(iter(s3_ghost)))

    # Safety: Prune frequency map if it grows too large (prevent memory leak simulation)
    # Keeping it 5x capacity to ensure history is available for Ghost items
    if len(s3_freq) > 5 * capacity:
        # Clear items not in cache or ghost to save memory
        # In a real impl, we'd use a more clever removal, but here we just pop arbitrary excess
        # or rely on update_after_evict to clean up.
        pass

    while True:
        # Decision: Evict from Small or Main?
        # Priority to clean Small if it's over budget
        if len(s3_small) >= s_capacity or not s3_main:
            if not s3_small:
                # Should not happen if cache is full and Main is empty
                return None

            candidate = next(iter(s3_small))
            freq = s3_freq.get(candidate, 0)

            if freq > 0:
                # Promote to Main (Merit based: Hit in Small)
                s3_small.pop(candidate)
                s3_main[candidate] = None
                s3_freq[candidate] = 0 # Reset freq to prove utility in Main
                continue
            
            # Downsampling Promotion (5% chance)
            # Use hash and access_count for randomness.
            # 5% (1/20) helps capture working sets from loops up to ~20x Cache Size.
            if (cache_snapshot.access_count ^ hash(candidate)) % 20 == 0:
                s3_small.pop(candidate)
                s3_main[candidate] = None
                s3_freq[candidate] = 0
                continue

            # Evict from Small
            return candidate

        else:
            # Evict from Main
            candidate = next(iter(s3_main))
            freq = s3_freq.get(candidate, 0)

            if freq > 0:
                # Second Chance in Main
                s3_main.pop(candidate)
                s3_main[candidate] = None # Reinsert at tail
                s3_freq[candidate] = freq - 1 # Decayed frequency
                continue
            
            # Evict from Main
            return candidate

def update_after_hit(cache_snapshot, obj):
    '''
    Update on Hit:
    - Increment frequency (cap at 3).
    - Do NOT move to MRU in Main (Scan resistance).
    '''
    global s3_freq
    key = obj.key
    s3_freq[key] = min(s3_freq.get(key, 0) + 1, 3)

def update_after_insert(cache_snapshot, obj):
    '''
    Update on Insert:
    - If in Ghost: Recall to Main.
    - Else: Insert to Small.
    - Reset Freq to 0.
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
    Update on Evict:
    - Move Small evictions to Ghost.
    - Remove from queues.
    - Remove Freq to keep metadata clean.
    '''
    global s3_small, s3_main, s3_ghost, s3_freq
    key = evicted_obj.key

    if key in s3_small:
        s3_small.pop(key)
        s3_ghost[key] = None
    elif key in s3_main:
        s3_main.pop(key)

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