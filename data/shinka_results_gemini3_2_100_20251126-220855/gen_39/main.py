# EVOLVE-BLOCK-START
"""S3-FIFO with Aging, Jitter, and Extended Ghost"""
import random

# S3-FIFO Metadata
# s3_small: FIFO queue for the small segment (probation)
# s3_main: FIFO queue for the main segment (protected)
# s3_ghost: Ghost cache for tracking eviction from small
# s3_freq: Frequency counter for objects (max 3)
s3_small = {}
s3_main = {}
s3_ghost = {}
s3_freq = {}
s3_next_aging = 0
last_access_count = -1

def _check_reset(cache_snapshot):
    global s3_small, s3_main, s3_ghost, s3_freq, s3_next_aging, last_access_count
    if cache_snapshot.access_count < last_access_count:
        s3_small.clear()
        s3_main.clear()
        s3_ghost.clear()
        s3_freq.clear()
        s3_next_aging = 0
    last_access_count = cache_snapshot.access_count

def _perform_aging():
    global s3_freq
    # Decay frequencies: divide by 2
    for k in list(s3_freq.keys()):
        s3_freq[k] >>= 1
        if s3_freq[k] == 0:
            # Optional cleanup to save memory, though get(k,0) handles missing keys
            pass

def evict(cache_snapshot, obj):
    '''
    S3-FIFO Eviction Policy with Multi-bit Clock and Aging:
    - Jittered Small partition to break loops.
    - Expanded Ghost cache for better rescue.
    - Main Queue with Frequency Aging.
    '''
    _check_reset(cache_snapshot)
    global s3_small, s3_main, s3_ghost, s3_freq

    capacity = cache_snapshot.capacity

    # Jitter: Randomize target Small size around 10% to prevent resonance
    # Range +/- 5% of capacity
    jitter_mag = max(1, int(capacity * 0.05))
    jitter = random.randint(-jitter_mag, jitter_mag)
    s_capacity = max(1, int(capacity * 0.1) + jitter)

    # Lazy cleanup of ghost - Expanded to 2x capacity for better loop capture
    while len(s3_ghost) > 2 * capacity:
        s3_ghost.pop(next(iter(s3_ghost)))

    while True:
        # Decision: Evict from Small or Main?
        if len(s3_small) >= s_capacity or not s3_main:
            if not s3_small:
                return None

            candidate = next(iter(s3_small))
            freq = s3_freq.get(candidate, 0)

            if freq > 0:
                # Hit in Small: Promote to Main
                s3_small.pop(candidate)
                s3_main[candidate] = None
                s3_freq[candidate] = 0 # Reset freq on promotion
                continue
            else:
                # Victim found in Small
                return candidate

        else:
            # Evict from Main
            candidate = next(iter(s3_main))
            freq = s3_freq.get(candidate, 0)

            if freq > 0:
                # Hit in Main: Decrement frequency and reinsert at tail
                s3_main.pop(candidate)
                s3_main[candidate] = None
                s3_freq[candidate] = freq - 1
                continue
            else:
                # Victim found in Main
                return candidate

def update_after_hit(cache_snapshot, obj):
    '''
    On Hit: Increment frequency (max 3).
    Check for global aging.
    '''
    _check_reset(cache_snapshot)
    global s3_freq, s3_next_aging

    s3_freq[obj.key] = min(s3_freq.get(obj.key, 0) + 1, 3)

    # Aging check
    if s3_next_aging == 0:
        s3_next_aging = cache_snapshot.access_count + cache_snapshot.capacity
    elif cache_snapshot.access_count >= s3_next_aging:
        _perform_aging()
        s3_next_aging = cache_snapshot.access_count + cache_snapshot.capacity

def update_after_insert(cache_snapshot, obj):
    '''
    On Insert: Handle Ghost/Small insertion.
    Check for global aging.
    '''
    _check_reset(cache_snapshot)
    global s3_small, s3_main, s3_ghost, s3_freq, s3_next_aging
    key = obj.key
    s3_freq[key] = 0

    if key in s3_ghost:
        s3_main[key] = None
        s3_ghost.pop(key)
    else:
        s3_small[key] = None

    # Aging check
    if s3_next_aging == 0:
        s3_next_aging = cache_snapshot.access_count + cache_snapshot.capacity
    elif cache_snapshot.access_count >= s3_next_aging:
        _perform_aging()
        s3_next_aging = cache_snapshot.access_count + cache_snapshot.capacity

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    On Evict: Remove from queues, add to Ghost (if from Small).
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