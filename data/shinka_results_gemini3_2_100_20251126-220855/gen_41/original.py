# EVOLVE-BLOCK-START
"""Cache eviction algorithm for optimizing hit rates across multiple workloads"""
from collections import OrderedDict
import random

# S3-FIFO with Randomized Eviction Global State
s3_small = OrderedDict()
s3_main = OrderedDict()
s3_ghost = OrderedDict()
s3_freq = {}
m_last_access_count = -1

def _check_reset(cache_snapshot):
    """Resets global state if a new trace is detected."""
    global s3_small, s3_main, s3_ghost, s3_freq, m_last_access_count
    if cache_snapshot.access_count < m_last_access_count:
        s3_small.clear()
        s3_main.clear()
        s3_ghost.clear()
        s3_freq.clear()
    m_last_access_count = cache_snapshot.access_count

def evict(cache_snapshot, obj):
    '''
    S3-FIFO with Randomized Eviction.
    - S (Small) buffers new items. M (Main) holds frequent items.
    - Victim is chosen from a small window at the head of S or M.
    - Randomization helps with synchronized loops.
    '''
    _check_reset(cache_snapshot)
    capacity = cache_snapshot.capacity
    target_s_size = max(1, int(capacity * 0.1))

    # Window size for scan/randomization
    k_window = 5

    while True:
        # Determine which queue to look at
        if len(s3_small) > target_s_size or len(s3_main) == 0:
            queue = s3_small
            is_small = True
        else:
            queue = s3_main
            is_small = False

        if not queue:
            # Should generally not happen if cache is full
            return next(iter(cache_snapshot.cache)) if cache_snapshot.cache else None

        # Get first K candidates
        candidates = []
        iterator = iter(queue)
        for _ in range(k_window):
            try:
                candidates.append(next(iterator))
            except StopIteration:
                break

        # Check for promotions (Lazy Promotion)
        promoted = False

        for key in candidates:
            if s3_freq.get(key, 0) > 0:
                # Promotion: Move to Main (or reinsert in Main)
                if is_small:
                    del s3_small[key]
                    s3_main[key] = 1
                else:
                    del s3_main[key]
                    s3_main[key] = 1

                s3_freq[key] = 0 # Reset frequency
                promoted = True
                break

        if promoted:
            continue

        # No promotions in window -> Pick random victim
        victim_key = random.choice(candidates)
        return victim_key

def update_after_hit(cache_snapshot, obj):
    '''
    On Hit: Increment frequency counter (capped).
    '''
    _check_reset(cache_snapshot)
    key = obj.key
    s3_freq[key] = min(3, s3_freq.get(key, 0) + 1)

def update_after_insert(cache_snapshot, obj):
    '''
    On Insert:
    - If in Ghost (G), insert to Main (M).
    - Else, insert to Small (S).
    '''
    _check_reset(cache_snapshot)
    key = obj.key

    if key in s3_ghost:
        # Recall from Ghost
        del s3_ghost[key]
        s3_main[key] = 1
        s3_freq[key] = 0
    else:
        # New Insert
        s3_small[key] = 1
        s3_freq[key] = 0

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    On Evict: Remove from queues and manage Ghost.
    '''
    key = evicted_obj.key

    if key in s3_small:
        del s3_small[key]
        # Evicted from S -> Add to G
        s3_ghost[key] = 1
    elif key in s3_main:
        del s3_main[key]
        # Evicted from M -> Discard (Standard S3-FIFO)

    if key in s3_freq:
        del s3_freq[key]

    # Limit Ghost size
    if len(s3_ghost) > cache_snapshot.capacity:
        s3_ghost.popitem(last=False)

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