# EVOLVE-BLOCK-START
"""
S3-FIFO Cache Eviction Algorithm Implementation.
Inspired by S3-FIFO (Simple, Scalable, Static) which uses a small FIFO queue (S)
and a main FIFO queue (M) with re-insertion to approximate LRU with scan resistance
and frequency awareness.
"""

from collections import OrderedDict

# Global metadata structures
# s_queue: Small FIFO queue (probationary)
# m_queue: Main FIFO queue (protected)
# ghost_registry: Ghost FIFO queue (history of evicted probationary items)
# access_counts: Dictionary mapping keys to frequency counters
# last_access_count: To detect trace reset
# insert_counter: For probabilistic admission

s_queue = OrderedDict()
m_queue = OrderedDict()
ghost_registry = OrderedDict()
access_counts = {}
last_access_count = -1
insert_counter = 0

def _reset_if_needed(snapshot):
    """Resets global state if a new trace execution is detected."""
    global s_queue, m_queue, ghost_registry, access_counts, last_access_count, insert_counter
    if snapshot.access_count < last_access_count:
        s_queue.clear()
        m_queue.clear()
        ghost_registry.clear()
        access_counts.clear()
        insert_counter = 0
    last_access_count = snapshot.access_count

def evict(cache_snapshot, obj):
    '''
    S3-FIFO eviction with Demotion, Frequency Decay, and Ghost History.
    '''
    global s_queue, m_queue, ghost_registry, access_counts
    _reset_if_needed(cache_snapshot)

    s_capacity = max(int(cache_snapshot.capacity * 0.1), 1)

    while True:
        # 1. Check Small Queue (Probation)
        if len(s_queue) > s_capacity or not m_queue:
            if not s_queue:
                if m_queue:
                    # Fallback to M if S is empty but M is not
                    pass
                else:
                    break # Both empty
            else:
                candidate_key, _ = s_queue.popitem(last=False)

                freq = access_counts.get(candidate_key, 0)
                if freq > 0:
                    # Promote to Main
                    m_queue[candidate_key] = None
                    access_counts[candidate_key] = 0 # Reset freq on promotion
                else:
                    # Evict from S -> Ghost
                    ghost_registry[candidate_key] = None
                    # Ghost management: Increased to 4x capacity for better scan/loop resistance
                    if len(ghost_registry) > cache_snapshot.capacity * 4:
                        ghost_registry.popitem(last=False)

                    if candidate_key in access_counts:
                        del access_counts[candidate_key]
                    return candidate_key
                continue

        # 2. Check Main Queue (Protected)
        candidate_key, _ = m_queue.popitem(last=False)

        freq = access_counts.get(candidate_key, 0)
        if freq > 0:
            # Re-insert in Main with decay
            m_queue[candidate_key] = None
            access_counts[candidate_key] = freq - 1
        else:
            # Demote to Small (Probation)
            s_queue[candidate_key] = None
            access_counts[candidate_key] = 0
            # Loop continues

    # Fallback
    if cache_snapshot.cache:
        return next(iter(cache_snapshot.cache))
    return None

def update_after_hit(cache_snapshot, obj):
    '''
    Increment frequency on hit, capped at 7.
    '''
    _reset_if_needed(cache_snapshot)
    curr = access_counts.get(obj.key, 0)
    access_counts[obj.key] = min(curr + 1, 7)

def update_after_insert(cache_snapshot, obj):
    '''
    Insert new object. Ghost hits go to Main.
    New items go to S, with probabilistic admission (scan guard) if S is full.
    '''
    global s_queue, m_queue, ghost_registry, access_counts, insert_counter
    _reset_if_needed(cache_snapshot)

    key = obj.key
    if key in ghost_registry:
        # Ghost Hit: Promote to Main
        m_queue[key] = None
        access_counts[key] = 0
        del ghost_registry[key]
    else:
        # New Insert
        s_capacity = max(int(cache_snapshot.capacity * 0.1), 1)

        # Scan Guard: If S is full, insert at HEAD (short probation) with high probability.
        # Use counter for determinism.
        if len(s_queue) >= s_capacity:
            insert_counter += 1
            # 25% chance to get normal probation (TAIL), 75% short probation (HEAD)
            if insert_counter % 4 == 0:
                s_queue[key] = None # TAIL
            else:
                s_queue[key] = None
                s_queue.move_to_end(key, last=False) # HEAD
        else:
            s_queue[key] = None # TAIL

        access_counts[key] = 0

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    Cleanup metadata.
    '''
    _reset_if_needed(cache_snapshot)
    k = evicted_obj.key
    if k in access_counts:
        del access_counts[k]
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