# EVOLVE-BLOCK-START
"""S3-FIFO with Randomized Window Eviction and Extended Ghost"""
from collections import OrderedDict
import random

# Global State
s3_small = {}
s3_main = {}
s3_ghost = {}
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
    S3-FIFO with Windowed Eviction Strategy.
    - Uses S3-FIFO queues (Small, Main, Ghost).
    - Checks a window of K items at the head of the selected queue.
    - Scans window for hot items to promote/reinsert immediately (Lazy Promotion).
    - If all items in window are cold, evicts a random victim from the window to reduce thrashing.
    '''
    global s3_small, s3_main, s3_ghost, s3_freq
    _check_reset(cache_snapshot)

    capacity = cache_snapshot.capacity
    s_capacity = max(1, int(capacity * 0.1))
    
    # Extended Ghost to capture larger loops (3x capacity)
    g_capacity = int(capacity * 3)

    # Lazy Ghost Cleanup
    while len(s3_ghost) > g_capacity:
        k = next(iter(s3_ghost))
        s3_ghost.pop(k)
        if k in s3_freq:
            del s3_freq[k]

    # Scan window size for eviction candidates
    k_window = 5

    while True:
        # 1. Select Queue (Small or Main)
        if len(s3_small) >= s_capacity or not s3_main:
            queue = s3_small
            source = 'small'
        else:
            queue = s3_main
            source = 'main'

        if not queue:
            # Fallback (should not happen in full cache)
            return None

        # 2. Get Window of Candidates
        candidates = []
        iterator = iter(queue)
        for _ in range(k_window):
            try:
                candidates.append(next(iterator))
            except StopIteration:
                break
        
        # 3. Check for Promotions (Hot items in window)
        promoted = False
        for key in candidates:
            freq = s3_freq.get(key, 0)
            if freq > 0:
                # Promotion / Maintenance
                if source == 'small':
                    # Hit in Small: Promote to Main
                    s3_small.pop(key)
                    s3_main[key] = None
                    s3_freq[key] = 0 # Reset probation
                else:
                    # Hit in Main: Clock reset
                    s3_main.pop(key)
                    s3_main[key] = None
                    s3_freq[key] = freq - 1 # Decrement freq
                
                promoted = True
                break # Restart decision loop to reflect state change
        
        if promoted:
            continue

        # 4. No hot items in window -> Evict random cold victim
        # Randomization helps break synchronized looping patterns
        return random.choice(candidates)

def update_after_hit(cache_snapshot, obj):
    '''On Hit: Increment frequency (capped at 3).'''
    _check_reset(cache_snapshot)
    key = obj.key
    s3_freq[key] = min(s3_freq.get(key, 0) + 1, 3)

def update_after_insert(cache_snapshot, obj):
    '''
    On Insert:
    - If in Ghost, rescue to Main with frequency boost.
    - Else, insert to Small.
    '''
    _check_reset(cache_snapshot)
    global s3_small, s3_main, s3_ghost, s3_freq
    key = obj.key

    if key in s3_ghost:
        # Rescue to Main
        s3_main[key] = None
        s3_ghost.pop(key)
        # Boost freq slightly (1) to give it a chance in Main
        s3_freq[key] = 1 
    else:
        # Insert to Small
        s3_small[key] = None
        s3_freq[key] = 0

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    On Evict:
    - Small eviction -> Ghost.
    - Main eviction -> Drop.
    '''
    global s3_small, s3_main, s3_ghost, s3_freq
    key = evicted_obj.key

    if key in s3_small:
        s3_small.pop(key)
        s3_ghost[key] = None
    elif key in s3_main:
        s3_main.pop(key)
        if key in s3_freq:
            del s3_freq[key]
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