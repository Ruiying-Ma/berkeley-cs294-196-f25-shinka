# EVOLVE-BLOCK-START
"""Cache eviction algorithm for optimizing hit rates across multiple workloads"""
from collections import OrderedDict
import random

# S3-FIFO with Extended Ghost and Anti-Thrashing
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
    S3-FIFO with Extended Ghost and Probabilistic LIFO.
    - Small: FIFO usually, but sometimes LIFO to break loops.
    - Main: LRU with Second Chance (Frequency).
    - Ghost: 2x Capacity to track history.
    '''
    _check_reset(cache_snapshot)
    capacity = cache_snapshot.capacity
    # Target S size: 10%
    target_s = max(1, int(capacity * 0.1))

    while True:
        # Decide queue to process
        process_small = False
        if len(s3_small) > target_s:
            process_small = True
        elif len(s3_main) == 0:
            process_small = True

        if process_small:
            if not s3_small:
                # Should be Main's turn if Small is empty
                process_small = False
            else:
                # Anti-Thrashing: Probabilistic LIFO eviction from Small
                # 1% chance to evict the newest item to break synchronized loops
                if random.random() < 0.01:
                    return next(reversed(s3_small))

                # Standard S3-FIFO Small Eviction (FIFO check)
                candidate = next(iter(s3_small))
                freq = s3_freq.get(candidate, 0)

                if freq > 0:
                    # Promote to Main
                    del s3_small[candidate]
                    s3_main[candidate] = None
                    s3_freq[candidate] = 0 # Reset freq on promotion
                    continue
                else:
                    return candidate

        if not process_small:
            if not s3_main:
                # Fallback
                return next(iter(cache_snapshot.cache))

            # Main Eviction (LRU with Second Chance)
            candidate = next(iter(s3_main))
            freq = s3_freq.get(candidate, 0)

            if freq > 0:
                # Second Chance: Reinsert at MRU, decrement freq
                s3_main.move_to_end(candidate)
                s3_freq[candidate] = freq - 1
                continue
            else:
                return candidate

def update_after_hit(cache_snapshot, obj):
    '''
    On Hit: Increment frequency.
    '''
    _check_reset(cache_snapshot)
    key = obj.key
    s3_freq[key] = min(3, s3_freq.get(key, 0) + 1)

def update_after_insert(cache_snapshot, obj):
    '''
    On Insert:
    - Check Ghost (Extended History).
    - Insert to Small or Main.
    '''
    _check_reset(cache_snapshot)
    key = obj.key

    if key in s3_ghost:
        # Ghost Hit: Restore to Main
        del s3_ghost[key]
        s3_main[key] = None
        # Restore frequency, possibly decayed?
        # Current logic: keep what was in s3_freq.
    else:
        # New Insert -> Small
        s3_small[key] = None
        s3_freq[key] = 0

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    On Evict: Move to Ghost. Keep frequency.
    '''
    key = evicted_obj.key

    if key in s3_small:
        del s3_small[key]
        s3_ghost[key] = None
    elif key in s3_main:
        del s3_main[key]
        s3_ghost[key] = None

    # Limit Ghost size (2x Capacity for extended history)
    # This helps with loops slightly larger than cache
    ghost_limit = cache_snapshot.capacity * 2
    while len(s3_ghost) > ghost_limit:
        k, _ = s3_ghost.popitem(last=False) # Remove oldest ghost
        if k in s3_freq:
            del s3_freq[k]

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