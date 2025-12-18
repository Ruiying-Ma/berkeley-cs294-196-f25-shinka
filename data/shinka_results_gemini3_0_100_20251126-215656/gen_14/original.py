# EVOLVE-BLOCK-START
"""S3-FIFO Cache Eviction Algorithm with Byte-Size Awareness"""
from collections import deque

# Global State
s_queue = deque()
m_queue = deque()
ghost_registry = set()
ghost_queue = deque()
access_bits = {}
s_bytes = 0
m_bytes = 0
last_access_count = -1

def check_reset(cache_snapshot):
    '''Reset globals if a new trace is detected'''
    global last_access_count, s_queue, m_queue, ghost_registry, ghost_queue, access_bits, s_bytes, m_bytes
    if cache_snapshot.access_count < last_access_count:
        s_queue.clear()
        m_queue.clear()
        ghost_registry.clear()
        ghost_queue.clear()
        access_bits.clear()
        s_bytes = 0
        m_bytes = 0
    last_access_count = cache_snapshot.access_count

def evict(cache_snapshot, obj):
    '''
    S3-FIFO Eviction Logic:
    - Maintains Small (S) and Main (M) queues.
    - S acts as a probation buffer (10% capacity).
    - M holds hot items.
    - Eviction candidates are chosen based on byte-size queue occupancy.
    '''
    check_reset(cache_snapshot)

    global s_queue, m_queue, access_bits, s_bytes, m_bytes

    cache_capacity = cache_snapshot.capacity
    target_s_bytes = cache_capacity * 0.1

    # Iterate until a victim is returned
    while True:
        # Rule 1: Evict from S if it's too big OR if M is empty
        if s_bytes >= target_s_bytes or not m_queue:
            if s_queue:
                candidate = s_queue[0]
                cand_obj = cache_snapshot.cache[candidate]
                cand_size = cand_obj.size

                # Check access bit (Lazy promotion)
                if access_bits.get(candidate, 0) > 0:
                    s_queue.popleft()
                    access_bits[candidate] = 0
                    m_queue.append(candidate)

                    # Update byte trackers
                    s_bytes -= cand_size
                    m_bytes += cand_size
                else:
                    # Found victim in S
                    return candidate
            else:
                # S empty, but condition triggered (likely M is empty too)
                pass

        # Rule 2: Evict from M
        if m_queue:
            candidate = m_queue[0]

            if access_bits.get(candidate, 0) > 0:
                # Second chance
                m_queue.rotate(-1)
                access_bits[candidate] = 0
            else:
                return candidate

        # Failsafe
        if not s_queue and not m_queue:
            return None

def update_after_hit(cache_snapshot, obj):
    '''Mark object as accessed'''
    check_reset(cache_snapshot)
    global access_bits
    access_bits[obj.key] = 1

def update_after_insert(cache_snapshot, obj):
    '''Insert into S or M based on Ghost history'''
    check_reset(cache_snapshot)
    global s_queue, m_queue, ghost_registry, access_bits, s_bytes, m_bytes

    key = obj.key
    size = obj.size
    access_bits[key] = 0

    if key in ghost_registry:
        # Rescue: Ghost -> M
        m_queue.append(key)
        m_bytes += size
        ghost_registry.remove(key)
    else:
        # New -> S
        s_queue.append(key)
        s_bytes += size

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''Cleanup queues and update ghost'''
    # No check_reset here usually, as it follows evict
    global s_queue, m_queue, ghost_registry, ghost_queue, access_bits, s_bytes, m_bytes

    key = evicted_obj.key
    size = evicted_obj.size

    # Remove from queues (expected to be at head of S or M from evict choice)
    if s_queue and s_queue[0] == key:
        s_queue.popleft()
        s_bytes -= size

        # S-eviction -> Ghost
        ghost_registry.add(key)
        ghost_queue.append(key)

        # Cap ghost size.
        # Using a dynamic limit proportional to current item count helps adaptation.
        current_item_count = len(s_queue) + len(m_queue)
        while len(ghost_registry) > max(current_item_count, 100):
             g = ghost_queue.popleft()
             if g in ghost_registry:
                 ghost_registry.remove(g)
                 break

    elif m_queue and m_queue[0] == key:
        m_queue.popleft()
        m_bytes -= size
        # M-eviction -> discard

    else:
        # Fallback (safety for consistency)
        if key in s_queue:
            s_queue.remove(key)
            s_bytes -= size
            ghost_registry.add(key)
            ghost_queue.append(key)
        elif key in m_queue:
            m_queue.remove(key)
            m_bytes -= size

    if key in access_bits:
        del access_bits[key]
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