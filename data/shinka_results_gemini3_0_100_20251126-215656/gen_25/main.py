# EVOLVE-BLOCK-START
"""
Dynamic S3-FIFO (DynS3FIFO)
An enhanced S3-FIFO algorithm with dynamic partition sizing (ARC-like adaptation).
Features:
1. Two queues: Small (S) and Main (M).
2. Ghost registries for both queues to track evicted items.
3. Dynamic S-queue sizing based on ghost hits (marginal utility).
4. Strict promotion: S -> M only on eviction + access bit.
5. Large ghost history (2x capacity) to catch larger loops.
"""

from collections import OrderedDict

# Global State
small_q = OrderedDict()       # Probationary queue (FIFO)
main_q = OrderedDict()        # Protected queue (FIFO)
ghost_s = OrderedDict()       # Ghost S (History of S evictions)
ghost_m = OrderedDict()       # Ghost M (History of M evictions)
accessed_bits = set()         # Track hits
s_dist = 0.1                  # Target fraction for S queue
last_access_count = 0         # For detecting trace changes

def check_reset(cache_snapshot):
    """Detects new trace and resets globals."""
    global small_q, main_q, ghost_s, ghost_m, accessed_bits, s_dist, last_access_count

    current_acc = cache_snapshot.access_count
    # Heuristic: access count reset/drop OR cache empty with residual state
    if current_acc < last_access_count or (len(cache_snapshot.cache) <= 1 and len(small_q) > 1):
        small_q.clear()
        main_q.clear()
        ghost_s.clear()
        ghost_m.clear()
        accessed_bits.clear()
        s_dist = 0.1
        last_access_count = 0

    last_access_count = current_acc

def evict(cache_snapshot, obj):
    '''
    Selects a victim using S3-FIFO with Demotion and Size-Gating.
    - Large items (>10% cap) get no second chance (Size-Gating).
    - Unaccessed M items are demoted to S instead of evicted (Demotion).
    '''
    global s_dist

    capacity = cache_snapshot.capacity
    s_target = max(1, int(capacity * s_dist))
    ghost_limit = max(capacity, int(2 * capacity))
    # Threshold for "Large" object: 10% of cache
    large_thresh = int(capacity * 0.1)

    while True:
        evict_s = (len(small_q) > s_target) or (len(main_q) == 0)

        if evict_s:
            if not small_q:
                evict_s = False
            else:
                key, _ = small_q.popitem(last=False)
                # Size check
                is_large = (cache_snapshot.cache[key].size > large_thresh)

                if key in accessed_bits and not is_large:
                    # Second Chance: Promote to M
                    accessed_bits.discard(key)
                    main_q[key] = None
                else:
                    # Evict from S
                    ghost_s[key] = None
                    if len(ghost_s) > ghost_limit:
                        ghost_s.popitem(last=False)
                    return key

        if not evict_s:
            if not main_q:
                return next(iter(cache_snapshot.cache))

            key, _ = main_q.popitem(last=False)
            is_large = (cache_snapshot.cache[key].size > large_thresh)

            if key in accessed_bits and not is_large:
                # Second Chance: Reinsert to M Tail
                accessed_bits.discard(key)
                main_q[key] = None
            else:
                # M Eviction Logic
                if is_large:
                    # Large items evicted directly to Ghost M
                    ghost_m[key] = None
                    if len(ghost_m) > ghost_limit:
                        ghost_m.popitem(last=False)
                    return key
                else:
                    # Demote to S (Second chance in probation)
                    accessed_bits.discard(key)
                    small_q[key] = None
                    # Loop continues (cache still full), likely triggers S eviction next

def update_after_hit(cache_snapshot, obj):
    check_reset(cache_snapshot)
    accessed_bits.add(obj.key)

def update_after_insert(cache_snapshot, obj):
    check_reset(cache_snapshot)

    global s_dist
    key = obj.key
    capacity = cache_snapshot.capacity

    # Adaptive sizing delta
    delta = 1.0 / capacity if capacity > 0 else 0.01

    if key in ghost_s:
        # Hit in Ghost S: S was too small. Increase S target.
        s_dist = min(0.9, s_dist + delta)
        # Rescue: promote to M
        main_q[key] = None
        del ghost_s[key]
        accessed_bits.discard(key) # Reset bit on rescue

    elif key in ghost_m:
        # Hit in Ghost M: M was too small. Decrease S target (Grow M).
        s_dist = max(0.01, s_dist - delta)
        # Rescue: promote to M
        main_q[key] = None
        del ghost_m[key]
        accessed_bits.discard(key)

    else:
        # New insert: Insert into S
        small_q[key] = None
        accessed_bits.discard(key)

def update_after_evict(cache_snapshot, obj, evicted_obj):
    # Ensure internal state matches cache state
    # (Victim was already popped in evict, but just in case of mismatch)
    key = evicted_obj.key
    accessed_bits.discard(key)
    if key in small_q:
        del small_q[key]
    if key in main_q:
        del main_q[key]
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