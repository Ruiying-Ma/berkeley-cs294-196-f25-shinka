# EVOLVE-BLOCK-START
"""S3-FIFO Cache Eviction Algorithm"""

from collections import OrderedDict

# Global S3-FIFO State
# S: Small queue (Probationary, initially 10% of cache)
# M: Main queue (Protected, initially 90% of cache)
# G: Ghost queue (History of evicted S items)
m_small = OrderedDict()
m_main = OrderedDict()
m_ghost = OrderedDict()
m_ghost_main = OrderedDict() # History of evicted M items
m_freq = {} # Frequency counters instead of binary set
m_last_access_count = 0
m_s_ratio = 0.1
MAX_FREQ = 3

def check_reset(cache_snapshot):
    global m_small, m_main, m_ghost, m_ghost_main, m_freq, m_last_access_count, m_s_ratio
    # Check for trace reset or new trace based on timestamp regression
    if cache_snapshot.access_count < m_last_access_count:
        m_small.clear()
        m_main.clear()
        m_ghost.clear()
        m_ghost_main.clear()
        m_freq.clear()
        m_s_ratio = 0.1
    m_last_access_count = cache_snapshot.access_count

def evict(cache_snapshot, obj):
    '''
    S3-FIFO eviction policy with Adaptive Sizing, Randomized Window and Frequency Counters.
    Includes Probabilistic LIFO to break loops.
    '''
    check_reset(cache_snapshot)
    global m_small, m_main, m_freq, m_s_ratio

    import itertools
    import random

    capacity = cache_snapshot.capacity
    # Adaptive target size for S
    target_s = max(1, int(capacity * m_s_ratio))

    while True:
        process_s = False
        if len(m_small) > target_s:
            process_s = True
        elif len(m_main) == 0:
            process_s = True

        if process_s:
            if not m_small:
                if m_main:
                    process_s = False
                else:
                    return next(iter(cache_snapshot.cache))

            if process_s:
                # Probabilistic LIFO (Anti-Thrashing)
                # Small chance to evict the newest item from Small to desynchronize loops
                if random.random() < 0.01:
                    return next(reversed(m_small))

                # Randomized Window Eviction: Check bottom K items
                k = 5
                window = list(itertools.islice(m_small, k))
                victim = None

                # Find first item with 0 frequency in window
                for key in window:
                    if m_freq.get(key, 0) == 0:
                        victim = key
                        break

                if victim:
                    return victim

                # All K items accessed? Process head.
                key = next(iter(m_small))
                freq = m_freq.get(key, 0)
                if freq > 0:
                    m_freq[key] = freq - 1
                    del m_small[key]
                    m_main[key] = None
                else:
                    return key

        if not process_s:
            if not m_main:
                continue

            # Randomized Window for M
            k = 5
            window = list(itertools.islice(m_main, k))
            victim = None

            for key in window:
                if m_freq.get(key, 0) == 0:
                    victim = key
                    break

            if victim:
                return victim

            # All K items accessed? Process head.
            key = next(iter(m_main))
            freq = m_freq.get(key, 0)
            if freq > 0:
                m_freq[key] = freq - 1
                m_main.move_to_end(key)
            else:
                return key

def update_after_hit(cache_snapshot, obj):
    check_reset(cache_snapshot)
    global m_freq
    # Increment frequency, capped at MAX_FREQ
    m_freq[obj.key] = min(MAX_FREQ, m_freq.get(obj.key, 0) + 1)

def update_after_insert(cache_snapshot, obj):
    '''
    Handle new insertion with adaptive sizing logic.
    '''
    check_reset(cache_snapshot)
    global m_small, m_main, m_ghost, m_ghost_main, m_freq, m_s_ratio

    key = obj.key
    # Adaptation logic: Adjust S target based on ghost hits
    delta = max(0.01, 1.0 / cache_snapshot.capacity) if cache_snapshot.capacity > 0 else 0.01

    if key in m_ghost:
        # Ghost S hit -> S was too small, increase S target
        m_s_ratio = min(0.9, m_s_ratio + delta)
        del m_ghost[key]
        m_main[key] = None
        # Restore frequency with decay
        if key in m_freq:
            m_freq[key] = m_freq[key] // 2
    elif key in m_ghost_main:
        # Ghost M hit -> M was too small (S too big), decrease S target
        m_s_ratio = max(0.01, m_s_ratio - delta)
        del m_ghost_main[key]
        m_main[key] = None
        # Restore frequency with decay
        if key in m_freq:
            m_freq[key] = m_freq[key] // 2
    else:
        # New Item: Insert into S
        m_small[key] = None
        # New item starts with freq 0
        m_freq[key] = 0

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    Clean up internal structures after eviction.
    Add evicted items to respective ghost lists.
    '''
    check_reset(cache_snapshot)
    global m_small, m_main, m_ghost, m_ghost_main, m_freq

    key = evicted_obj.key

    if key in m_small:
        del m_small[key]
        m_ghost[key] = None
    elif key in m_main:
        del m_main[key]
        m_ghost_main[key] = None

    # Manage Ghost sizes (Extended capacity to 3x to catch larger loops)
    cap = cache_snapshot.capacity * 3
    while len(m_ghost) > cap:
        k, _ = m_ghost.popitem(last=False)
        if k in m_freq:
            del m_freq[k]
    while len(m_ghost_main) > cap:
        k, _ = m_ghost_main.popitem(last=False)
        if k in m_freq:
            del m_freq[k]

    # Note: We do NOT delete from m_freq here if it goes to ghost.
    # We want to preserve history for restoration.
    # For safety, if key is not in either ghost (rare/impossible here), remove freq.
    if key not in m_ghost and key not in m_ghost_main:
        if key in m_freq:
            del m_freq[key]
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