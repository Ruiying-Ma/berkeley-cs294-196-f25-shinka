# EVOLVE-BLOCK-START
"""S3-FIFO with Multi-bit Frequency Counters and Trace Reset"""

# Global Metadata
# Using standard dicts (ordered in recent Python) for queues
s3_small = {}
s3_main = {}
s3_ghost = {}
s3_freq = {}
m_last_access_count = -1

def _check_reset(cache_snapshot):
    """Resets global state if a new trace is detected."""
    global s3_small, s3_main, s3_ghost, s3_freq, m_last_access_count
    # If access_count decreased, we started a new trace
    if cache_snapshot.access_count < m_last_access_count:
        s3_small.clear()
        s3_main.clear()
        s3_ghost.clear()
        s3_freq.clear()
    m_last_access_count = cache_snapshot.access_count

def evict(cache_snapshot, obj):
    '''
    S3-FIFO Eviction Policy with Multi-bit Clock.
    - S (Small): FIFO. Hits promote to Main.
    - M (Main): Clock with 2-bit frequency (0-3).
      - Items in M are evicted only if freq is 0.
      - If freq > 0, decrement and reinsert (move to tail).
    - G (Ghost): Tracks history for rescue. Extended size.
    '''
    global s3_small, s3_main, s3_ghost, s3_freq
    _check_reset(cache_snapshot)

    capacity = cache_snapshot.capacity
    # Target size for Small queue (10% of capacity)
    s_capacity = max(1, int(capacity * 0.1))

    # Extended Ghost: 2x Capacity to track loops/scans better
    g_capacity = int(capacity * 2)

    # Lazy cleanup of ghost
    while len(s3_ghost) > g_capacity:
        # Remove oldest item (head of dict)
        k = next(iter(s3_ghost))
        s3_ghost.pop(k)
        # Ensure freq is removed if present
        if k in s3_freq:
            del s3_freq[k]

    while True:
        # Decision: Evict from Small or Main?
        # Rule: Evict from Small if it exceeds its target size, OR if Main is empty.
        if len(s3_small) >= s_capacity or not s3_main:
            if not s3_small:
                return None

            candidate = next(iter(s3_small))
            freq = s3_freq.get(candidate, 0)

            if freq > 0:
                # Hit in Small: Promote to Main
                # Reset frequency to 0 upon entering Main (new probation)
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
                # Hit in Main: Decrement frequency and reinsert at tail (Clock logic)
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
    '''
    _check_reset(cache_snapshot)
    key = obj.key
    s3_freq[key] = min(s3_freq.get(key, 0) + 1, 3)

def update_after_insert(cache_snapshot, obj):
    '''
    On Insert:
    - If in Ghost, insert to Main (rescue) with initial freq boost.
    - Else, insert to Small.
    '''
    _check_reset(cache_snapshot)
    global s3_small, s3_main, s3_ghost, s3_freq
    key = obj.key

    if key in s3_ghost:
        # Rescue: Ghost -> Main
        s3_main[key] = None
        s3_ghost.pop(key)
        # Give restored items a small frequency boost (1)
        s3_freq[key] = 1
    else:
        # Insert New: Small
        s3_small[key] = None
        s3_freq[key] = 0

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    On Evict:
    - Cleanup from queues.
    - If evicted from Small OR Main, add to Ghost to track history.
    '''
    global s3_small, s3_main, s3_ghost, s3_freq
    key = evicted_obj.key

    if key in s3_small:
        s3_small.pop(key)
        s3_ghost[key] = None
    elif key in s3_main:
        s3_main.pop(key)
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