# EVOLVE-BLOCK-START
"""S3-FIFO with Multi-bit Frequency, Jitter, and Windowed Eviction"""
import random
import itertools

# Global State
# s3_small: FIFO queue for the small segment (probation)
# s3_main: LRU queue for the main segment (protected)
# s3_ghost: Ghost cache for tracking eviction from small
# s3_freq: Frequency counter for objects (max 3)
s3_small = {}
s3_main = {}
s3_ghost = {}
s3_freq = {}
s3_last_access_count = -1

def _check_reset(cache_snapshot):
    global s3_small, s3_main, s3_ghost, s3_freq, s3_last_access_count
    if cache_snapshot.access_count < s3_last_access_count:
        s3_small.clear()
        s3_main.clear()
        s3_ghost.clear()
        s3_freq.clear()
    s3_last_access_count = cache_snapshot.access_count

def evict(cache_snapshot, obj):
    '''
    S3-FIFO with Multi-bit Frequency, Jitter, and Windowed Eviction.

    Combines:
    1. S3-FIFO structure (Small/Main/Ghost).
    2. Multi-bit frequency counters (0-3).
    3. Jittered Small queue capacity.
    4. Windowed Scanning to reduce Head-of-Line blocking.
    5. Randomized victim selection in Small queue.
    6. Extended Ghost Registry (3x) to catch large loops (Trace 14).
    '''
    _check_reset(cache_snapshot)
    global s3_small, s3_main, s3_ghost, s3_freq

    capacity = cache_snapshot.capacity

    # 1. Jittered S Capacity
    # Dynamic target size for Small queue (10% +/- 1%)
    noise_range = max(1, int(capacity * 0.01))
    s_capacity = max(1, int(capacity * 0.1) + random.randint(-noise_range, noise_range))

    # 2. Ghost Cleanup
    # Extended capacity (3x) to track history for large loops
    while len(s3_ghost) > 3 * capacity:
        s3_ghost.pop(next(iter(s3_ghost)))

    # Window size for scanning candidates
    k_window = 5

    while True:
        # 3. Queue Selection
        # Evict from Small if over capacity or Main is empty
        if len(s3_small) >= s_capacity or not s3_main:
            queue = s3_small
            is_small = True
        else:
            queue = s3_main
            is_small = False

        if not queue:
            # Should not happen in a full cache unless empty
            return None

        # 4. Window Scan
        # Peek at the first K items in the queue
        candidates = list(itertools.islice(queue, k_window))

        # 5. Check for Promotions / Maintenance
        # Look for *any* item with frequency > 0 in the window.
        promoted = False

        for key in candidates:
            freq = s3_freq.get(key, 0)
            if freq > 0:
                # Hot item found: Promote or Reinsert
                if is_small:
                    # S -> M Promotion
                    s3_small.pop(key)
                    s3_main[key] = None
                    s3_freq[key] = 0 # Reset freq on promotion
                else:
                    # M -> M Reinsertion (give second chance)
                    s3_main.pop(key)
                    s3_main[key] = None # Move to MRU
                    s3_freq[key] = freq - 1 # Decay frequency

                promoted = True
                break # Restart loop to reflect state change

        if promoted:
            continue

        # 6. Victim Selection
        # If we are here, no items in the window had freq > 0.
        # Select a victim from these cold candidates.

        if is_small:
            # In Small Queue: Pick a RANDOM victim from the window.
            # This randomness helps break pathological loops (Trace 14, 29).
            return random.choice(candidates)
        else:
            # In Main Queue: Pick the HEAD (Strict LRU).
            # Candidates are ordered, so candidates[0] is the LRU item.
            return candidates[0]

def update_after_hit(cache_snapshot, obj):
    '''
    On Hit:
    - Increment frequency (capped at 3).
    - If in Main, move to MRU (True LRU behavior).
    '''
    _check_reset(cache_snapshot)
    global s3_freq, s3_main
    key = obj.key
    s3_freq[key] = min(s3_freq.get(key, 0) + 1, 3)

    if key in s3_main:
        # Move to MRU
        val = s3_main.pop(key)
        s3_main[key] = val

def update_after_insert(cache_snapshot, obj):
    '''
    On Insert:
    - Check Ghost to decide S vs M.
    - If Ghost Hit: Restore to Main with bonus frequency (Recurrence).
    - Else: Insert to Small with 0 frequency.
    '''
    _check_reset(cache_snapshot)
    global s3_small, s3_main, s3_ghost, s3_freq
    key = obj.key

    if key in s3_ghost:
        # Ghost hit: Rescue to Main
        s3_main[key] = None
        s3_ghost.pop(key)
        # Grant a "recurrence bonus" (freq=1) to survive one eviction scan
        s3_freq[key] = 1
    else:
        # New insert: Enter Small (Probation)
        s3_small[key] = None
        s3_freq[key] = 0

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    On Evict:
    - Remove from internal queues.
    - Track BOTH Small and Main evictions in Ghost to capture long-loop patterns.
    '''
    global s3_small, s3_main, s3_ghost, s3_freq
    key = evicted_obj.key

    if key in s3_small:
        s3_small.pop(key)
        s3_ghost[key] = None
    elif key in s3_main:
        s3_main.pop(key)
        s3_ghost[key] = None # Added: Track Main evictions too

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