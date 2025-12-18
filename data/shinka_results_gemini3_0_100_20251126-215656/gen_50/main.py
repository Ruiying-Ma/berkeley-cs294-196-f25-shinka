# EVOLVE-BLOCK-START
from collections import OrderedDict

# Tiered S3-FIFO with Ghost-Guided Promotion
# s_q: Small Queue (FIFO) - Probation for new items
# m_q: Main Queue (LRU) - Protected for popular items
# g_q: Ghost Registry (FIFO) - History of evicted items
# counts: Frequency tracking

q_s = OrderedDict()
q_m = OrderedDict()
q_g = OrderedDict()
counts = {}
last_ts = -1

def _reset(snapshot):
    global q_s, q_m, q_g, counts, last_ts
    if snapshot.access_count < last_ts:
        q_s.clear(); q_m.clear(); q_g.clear(); counts.clear()
    if not snapshot.cache and (q_s or q_m):
        q_s.clear(); q_m.clear(); q_g.clear(); counts.clear()
    last_ts = snapshot.access_count

def evict(cache_snapshot, obj):
    '''
    Tiered S3-FIFO Eviction.
    - S Queue: Requires 2 hits (freq>=2) to promote to M.
               1 hit (freq=1) grants a second chance in S.
               0 hits (freq=0) -> Ghost.
    - M Queue: Hits (freq>0) -> Demote to S (with freq=1).
               No hits -> Evict to Ghost.
    '''
    global q_s, q_m, q_g, counts
    _reset(cache_snapshot)

    cap = cache_snapshot.capacity
    s_cap = max(1, int(cap * 0.1))
    
    # Loop to find a victim
    # We limit iterations to avoid potential infinite loops, though logic should converge
    for _ in range(cap * 2 + 100):
        # Determine which queue to scan
        # Scan S if over capacity OR if M is empty
        scan_s = False
        if len(q_s) > s_cap or not q_m:
            scan_s = True
        
        if scan_s:
            if not q_s:
                if q_m: scan_s = False # Fallback to M
                else: break # Both empty
            
            if scan_s:
                k, _ = q_s.popitem(last=False) # FIFO
                f = counts.get(k, 0)
                
                if f >= 2:
                    # Promote to Main
                    q_m[k] = None
                    counts[k] = 0 # Reset freq on promotion
                elif f == 1:
                    # Reinsert in Small (Second Chance)
                    q_s[k] = None
                    counts[k] = 0 # Reset freq so it needs a hit to survive again
                else:
                    # Evict
                    q_g[k] = None
                    counts.pop(k, None)
                    # Ghost Cap (4x to capture large loops)
                    if len(q_g) > cap * 4:
                        q_g.popitem(last=False)
                    return k
        else:
            # Scan M
            k, _ = q_m.popitem(last=False) # LRU Head
            
            f = counts.get(k, 0)
            if f > 0:
                # Demote to Small
                q_s[k] = None
                counts[k] = 1 # Give freq=1 (survives 1 pass in S)
            else:
                # Evict
                q_g[k] = None
                counts.pop(k, None)
                if len(q_g) > cap * 4:
                    q_g.popitem(last=False)
                return k

    # Ultimate fallback
    if cache_snapshot.cache:
        return next(iter(cache_snapshot.cache))
    return None

def update_after_hit(cache_snapshot, obj):
    global q_m, counts
    _reset(cache_snapshot)
    k = obj.key
    counts[k] = min(counts.get(k, 0) + 1, 3)
    if k in q_m:
        q_m.move_to_end(k) # MRU update

def update_after_insert(cache_snapshot, obj):
    global q_s, q_m, q_g, counts
    _reset(cache_snapshot)
    k = obj.key
    
    if k in q_g:
        # Ghost Hit: Promote to Main
        # We trust the Ghost signal (recurrence)
        del q_g[k]
        q_m[k] = None
        counts[k] = 0
    else:
        # New Item: Insert to Small
        q_s[k] = None
        counts[k] = 0

def update_after_evict(cache_snapshot, obj, evicted_obj):
    global q_s, q_m, counts
    _reset(cache_snapshot)
    k = evicted_obj.key
    q_s.pop(k, None)
    q_m.pop(k, None)
    counts.pop(k, None)
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