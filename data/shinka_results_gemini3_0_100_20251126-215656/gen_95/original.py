# EVOLVE-BLOCK-START
from collections import OrderedDict

# S3-FIFO with Demotion and State Reset (s3fifo_robust_demotion)
# Combines S3-FIFO-D logic with robust state management.
# - s_queue: Small/Probationary FIFO queue (10% capacity)
# - m_queue: Main/Protected FIFO queue (90% capacity)
# - g_queue: Ghost FIFO queue (2x capacity)
# - freq: Frequency counter map (capped at 3)

s_queue = OrderedDict()
m_queue = OrderedDict()
g_queue = OrderedDict()
freq = {}
last_ts = -1

def _reset(snapshot):
    """
    Resets global state if a new trace execution is detected.
    """
    global s_queue, m_queue, g_queue, freq, last_ts
    # If time went backwards, we are on a new trace
    if snapshot.access_count < last_ts:
        s_queue.clear()
        m_queue.clear()
        g_queue.clear()
        freq.clear()
    last_ts = snapshot.access_count

def evict(cache_snapshot, obj):
    """
    S3-FIFO Eviction with Demotion.
    """
    global s_queue, m_queue, g_queue, freq
    _reset(cache_snapshot)

    cap = cache_snapshot.capacity
    s_cap = max(int(cap * 0.1), 1)

    while True:
        # Check Small Queue (Probation)
        # Process Small if it exceeds budget OR if Main is empty (force fill Main)
        if len(s_queue) > s_cap or not m_queue:
            if not s_queue:
                # Fallback: If Small is empty here, Main must also be empty (from condition).
                # This implies cache is effectively empty or desynced.
                if m_queue:
                    # Should unlikely happen, but fallback to Main
                    k, _ = m_queue.popitem(last=False)
                    freq.pop(k, None)
                    return k
                # Last resort fallback
                if cache_snapshot.cache:
                    return next(iter(cache_snapshot.cache))
                return None

            k, _ = s_queue.popitem(last=False) # FIFO Pop
            f = freq.get(k, 0)
            
            if f > 0:
                # Promotion: Small -> Main
                # Item was accessed while in probation. Move to Protected.
                m_queue[k] = None
                freq[k] = 0 # Reset frequency on promotion
            else:
                # Eviction: Small -> Ghost
                # No hits in probation. Evict and track in Ghost.
                g_queue[k] = None
                freq.pop(k, None) # Remove from active freq map
                
                # Cap Ghost size
                if len(g_queue) > cap * 2:
                    g_queue.popitem(last=False)
                return k
        else:
            # Check Main Queue (Protected)
            # Small is within budget, so we clean Main (LRU approximation)
            k, _ = m_queue.popitem(last=False) # FIFO Pop
            f = freq.get(k, 0)
            
            if f > 0:
                # Reinsertion: Main -> Main
                # Item has hits. Give it a second chance (move to tail) and decay frequency.
                m_queue[k] = None
                freq[k] = f - 1
            else:
                # Demotion: Main -> Small
                # Item was cold in Main. Give it one last chance in Small.
                s_queue[k] = None
                freq[k] = 0

def update_after_hit(cache_snapshot, obj):
    """
    Increment frequency on hit, capped at 3.
    """
    global freq
    _reset(cache_snapshot)
    freq[obj.key] = min(freq.get(obj.key, 0) + 1, 3)

def update_after_insert(cache_snapshot, obj):
    """
    Handle new insertions.
    Ghost hits -> Main.
    New items -> Small.
    """
    global s_queue, m_queue, g_queue, freq
    _reset(cache_snapshot)
    
    k = obj.key
    freq[k] = 0 # Initialize frequency
    
    if k in g_queue:
        # Ghost Hit: Item returned shortly after eviction (Loop).
        # Promote directly to Main.
        m_queue[k] = None
        del g_queue[k]
    else:
        # New Insert: Place in Probation (Small).
        s_queue[k] = None

def update_after_evict(cache_snapshot, obj, evicted_obj):
    """
    Cleanup frequency map for the evicted object.
    """
    global freq
    # The evicted_obj key should be removed from freq map if not already
    if evicted_obj.key in freq:
        del freq[evicted_obj.key]
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