# EVOLVE-BLOCK-START
"""S3-FIFO with Adaptive Ghost and Hybrid LRU/MRU Main Eviction"""

# Global Metadata
# s3_small: FIFO queue for new items (probation)
# s3_main: Main queue (OrderedDict-like) for promoted items
# s3_ghost: Ghost cache tracking evicted items
# s3_freq: Frequency counters (0-3)
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
    S3-FIFO Eviction with Hybrid LRU/MRU Policy:
    1. Ghost Cleanup: Maintains a large ghost registry (4x capacity).
    2. Small Queue: Evicts to Ghost if full. Hits promote to Main.
    3. Main Queue:
       - Checks Head (LRU position).
       - If Head is Hot (freq > 0): Decrement and Move to Tail (Second Chance).
       - If Head is Cold (freq == 0):
         - Check Tail (MRU position).
         - If Tail is also Cold (freq == 0): Evict Tail (MRU).
           - This protects Old Cold items (Head) from New Cold items (Tail).
           - Handles Loops and Scans better.
         - Else (Tail is Hot): Evict Head (LRU).
    '''
    global s3_small, s3_main, s3_ghost, s3_freq
    _check_reset(cache_snapshot)

    capacity = cache_snapshot.capacity
    # Target size for Small queue (10% of capacity)
    s_capacity = max(1, int(capacity * 0.1))
    
    # Extended Ghost Capacity: 4x cache size for large loops
    g_capacity = int(capacity * 4)

    # Lazy cleanup of ghost
    while len(s3_ghost) > g_capacity:
        k = next(iter(s3_ghost))
        s3_ghost.pop(k)
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
                s3_small.pop(candidate)
                s3_main[candidate] = None
                s3_freq[candidate] = 0 # Reset freq (Probation in Main)
                continue
            else:
                # Victim found in Small
                return candidate

        else:
            # Evict from Main
            head_key = next(iter(s3_main))
            head_freq = s3_freq.get(head_key, 0)

            if head_freq > 0:
                # Head is Hot: Second Chance
                s3_main.pop(head_key)
                s3_main[head_key] = None
                s3_freq[head_key] = head_freq - 1
                continue
            else:
                # Head is Cold (freq 0). 
                # Check Tail (MRU) to decide between LRU and MRU eviction.
                tail_key = next(reversed(s3_main))
                tail_freq = s3_freq.get(tail_key, 0)

                if tail_freq == 0:
                    # Tail is also Cold. Evict Tail (MRU).
                    # This implies the item just entered Main (or was accessed long ago and moved to tail 
                    # but decayed), and has no hits since.
                    # Prefer evicting New Cold (Tail) over Old Cold (Head).
                    return tail_key
                else:
                    # Tail is Hot. We cannot evict it.
                    # Evict Head (LRU) as it is Cold.
                    return head_key

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
    - If in Ghost, rescue to Main with Boosted Frequency.
    - Else, insert to Small.
    '''
    _check_reset(cache_snapshot)
    global s3_small, s3_main, s3_ghost, s3_freq
    key = obj.key
    
    if key in s3_ghost:
        # Rescue: Ghost -> Main
        s3_main[key] = None
        s3_ghost.pop(key)
        # Boost freq to 2 (Hot).
        # This prevents the "Tail is Cold" check from immediately evicting it
        # (since new items enter at Tail).
        s3_freq[key] = 2
    else:
        # Insert New: Small
        s3_small[key] = None
        s3_freq[key] = 0

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    On Evict:
    - Add to Ghost to track history.
    '''
    global s3_small, s3_main, s3_ghost, s3_freq
    key = evicted_obj.key

    if key in s3_small:
        s3_small.pop(key)
        s3_ghost[key] = None
    elif key in s3_main:
        s3_main.pop(key)
        s3_ghost[key] = None
    
    # Remove freq tracking to clean up, unless we decide to keep it in ghost.
    # Here we reset freq on rescue anyway (to 2), so we don't need old freq.
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