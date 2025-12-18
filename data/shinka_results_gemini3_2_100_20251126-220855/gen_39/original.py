# EVOLVE-BLOCK-START
"""S3-FIFO with Multi-bit Frequency Counters"""

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
    S3-FIFO Eviction Policy with Multi-bit Clock:
    - Small Queue (S): FIFO. Hits promote to Main.
    - Main Queue (M): Segmented FIFO with frequency counters.
      - Items in M have a frequency count (0-3).
      - Eviction from M decrements frequency. Only evict if freq is 0.
      - This approximates a multi-bit clock / LFU-like retention.
    - Ghost Queue (G): Tracks items evicted from S to rescue them quickly.
    '''
    global s3_small, s3_main, s3_ghost, s3_freq

    capacity = cache_snapshot.capacity
    # Target size for Small queue (10% of capacity)
    s_capacity = max(1, int(capacity * 0.1))

    # Lazy cleanup of ghost
    while len(s3_ghost) > capacity:
        s3_ghost.pop(next(iter(s3_ghost)))

    while True:
        # Decision: Evict from Small or Main?
        # Rule: Evict from Small if it exceeds its target size, OR if Main is empty.
        if len(s3_small) >= s_capacity or not s3_main:
            if not s3_small:
                # Safety valve: if Main is empty and Small is empty (should not happen in full cache)
                return None

            candidate = next(iter(s3_small))
            freq = s3_freq.get(candidate, 0)

            if freq > 0:
                # Hit in Small: Promote to Main
                # Reset frequency to 0 upon entering Main (probation passed)
                # This ensures it doesn't stay in Main forever without new hits
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
                # Hit in Main: Decrement frequency and reinsert at tail (Second/Third Chance)
                # This distinguishes between "hot" (freq=3) and "warm" (freq=1) items.
                s3_main.pop(candidate)
                s3_main[candidate] = None
                s3_freq[candidate] = freq - 1
                continue
            else:
                # Victim found in Main
                return candidate

def update_after_hit(cache_snapshot, obj):
    '''
    On Hit: Increment frequency, capped at 3.
    This helps identify frequently accessed items.
    '''
    global s3_freq
    s3_freq[obj.key] = min(s3_freq.get(obj.key, 0) + 1, 3)

def update_after_insert(cache_snapshot, obj):
    '''
    On Insert:
    - If in Ghost, insert to Main (rescue).
    - Else, insert to Small.
    - Initialize frequency to 0.
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
    On Evict:
    - Cleanup from queues.
    - If evicted from Small, add to Ghost.
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