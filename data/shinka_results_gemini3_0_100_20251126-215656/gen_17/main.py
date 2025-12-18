# EVOLVE-BLOCK-START
from collections import deque

# S3-FIFO Algorithm Implementation
# S: Small Queue, M: Main Queue, G: Ghost Registry
s3_s = deque()
s3_m = deque()
s3_g = set()
s3_g_fifo = deque()
s3_access = {}
s3_last_access_count = -1

def _check_reset(cache_snapshot):
    global s3_s, s3_m, s3_g, s3_g_fifo, s3_access, s3_last_access_count
    if cache_snapshot.access_count < s3_last_access_count:
        s3_s.clear()
        s3_m.clear()
        s3_g.clear()
        s3_g_fifo.clear()
        s3_access.clear()

    if len(cache_snapshot.cache) == 0 and (len(s3_s) > 0 or len(s3_m) > 0):
        s3_s.clear()
        s3_m.clear()
        s3_g.clear()
        s3_g_fifo.clear()
        s3_access.clear()

    s3_last_access_count = cache_snapshot.access_count

def evict(cache_snapshot, obj):
    '''
    S3-FIFO eviction logic.
    '''
    _check_reset(cache_snapshot)

    c = cache_snapshot.capacity
    s_cap = max(1, int(c * 0.1))

    victim = None

    # Iterate until a victim is found
    while victim is None:
        scan_s = False
        # If S is full or M is empty, scan S
        if len(s3_s) > s_cap or len(s3_m) == 0:
            scan_s = True

        if scan_s:
            if not s3_s:
                # Fallback if S is empty but we thought we should scan it
                if s3_m: scan_s = False
                else: break

            if scan_s:
                cand = s3_s.popleft()
                if s3_access.get(cand, False):
                    # Second chance: promote to M
                    s3_access[cand] = False
                    s3_m.append(cand)
                else:
                    # Evict from S -> Ghost
                    victim = cand
                    if victim not in s3_g:
                        s3_g.add(victim)
                        s3_g_fifo.append(victim)
                        # Limit ghost size to 3x capacity
                        while len(s3_g) > 3 * c:
                            rem = s3_g_fifo.popleft()
                            if rem in s3_g: s3_g.remove(rem)
        else:
            # Scan M
            cand = s3_m.popleft()
            if s3_access.get(cand, False):
                # Second chance: reinsert to M
                s3_access[cand] = False
                s3_m.append(cand)
            else:
                # Evict from M -> Discard (no ghost for M eviction in vanilla S3-FIFO)
                victim = cand

    if victim is None:
        # Failsafe
        if cache_snapshot.cache:
            victim = next(iter(cache_snapshot.cache))

    return victim

def update_after_hit(cache_snapshot, obj):
    '''
    Update access bit on hit.
    '''
    _check_reset(cache_snapshot)
    s3_access[obj.key] = True

def update_after_insert(cache_snapshot, obj):
    '''
    Handle insertion (miss).
    '''
    _check_reset(cache_snapshot)
    key = obj.key

    if key in s3_g:
        # Ghost hit: Promote to M
        s3_m.append(key)
        s3_access[key] = True
        s3_g.remove(key)
        # Note: key remains in s3_g_fifo but ignored on pop
    else:
        # Standard insert to S
        s3_s.append(key)
        s3_access[key] = False

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    Cleanup after eviction.
    '''
    _check_reset(cache_snapshot)
    key = evicted_obj.key
    if key in s3_access:
        del s3_access[key]
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