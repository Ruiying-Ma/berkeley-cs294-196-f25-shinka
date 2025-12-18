# EVOLVE-BLOCK-START
"""Cache eviction algorithm combining ARC adaptive sizing with S3 probabilistic rescue"""
from collections import OrderedDict
import random

# Global State
m_t1 = OrderedDict() # T1: Recent cache entries (Probation)
m_t2 = OrderedDict() # T2: Frequent cache entries (Protected)
m_b1 = OrderedDict() # B1: Ghost entries evicted from T1
m_b2 = OrderedDict() # B2: Ghost entries evicted from T2
m_p = 0              # Target size for T1
m_last_access_count = -1

def _check_reset(cache_snapshot):
    """Resets global state if a new trace is detected based on access count rollback."""
    global m_t1, m_t2, m_b1, m_b2, m_p, m_last_access_count
    if cache_snapshot.access_count < m_last_access_count:
        m_t1.clear()
        m_t2.clear()
        m_b1.clear()
        m_b2.clear()
        m_p = 0
    m_last_access_count = cache_snapshot.access_count

def evict(cache_snapshot, obj):
    '''
    Hybrid ARC Eviction:
    - Adaptive 'p' parameter from ARC.
    - Randomized victim selection from T1 (from Inspiration).
    - Probabilistic 'Lucky Save' from S3 to prevent thrashing in T1.
    '''
    _check_reset(cache_snapshot)
    global m_p

    key = obj.key
    capacity = cache_snapshot.capacity

    # 1. Adapt p (target size of T1) based on hits in ghost lists
    if key in m_b1:
        delta = 1
        if len(m_b1) > 0 and len(m_b2) > 0:
            delta = max(1, len(m_b2) / len(m_b1))
        m_p = min(capacity, m_p + delta)
    elif key in m_b2:
        delta = 1
        if len(m_b1) > 0 and len(m_b2) > 0:
            delta = max(1, len(m_b1) / len(m_b2))
        m_p = max(0, m_p - delta)

    # 2. Select victim source (T1 vs T2)
    # Apply small jitter to p for boundary fuzzing (inspired by S3)
    # This prevents the algorithm from getting stuck at a hard threshold
    p_jitter = random.randint(-1, 1) if capacity > 10 else 0
    target_t1 = max(0, min(capacity, m_p + p_jitter))

    evict_t1 = False
    if len(m_t1) > 0:
        if len(m_t1) > target_t1:
            evict_t1 = True
        elif key in m_b2 and len(m_t1) >= int(target_t1):
            evict_t1 = True

    # If T2 is empty, we must evict from T1
    if len(m_t2) == 0:
        evict_t1 = True

    # 3. Select specific victim
    if evict_t1 and m_t1:
        # Loop to allow for 'Lucky Save' retries
        # Try up to 3 times to find a victim
        for _ in range(3):
            # Randomized selection from bottom k (Scan resistance)
            k = 5
            candidates = []
            it = iter(m_t1)
            try:
                for _ in range(k):
                    candidates.append(next(it))
            except StopIteration:
                pass

            victim = random.choice(candidates)

            # Lucky Save (from S3): Small chance to give second chance
            # Helps retain items during transient scans/loops
            if random.random() < 0.05: # 5% chance
                m_t1.move_to_end(victim) # Move to MRU (Rescue)
                # Victim is now at end, next loop will pick different candidates
                continue
            else:
                return victim

        # Fallback if we looped out (pick LRU)
        return next(iter(m_t1))
    else:
        # T2 Eviction: LRU
        # T2 contains high-value items, LRU is usually optimal here
        return next(iter(m_t2)) if m_t2 else next(iter(m_t1))

def update_after_hit(cache_snapshot, obj):
    '''
    On Cache Hit:
    - If in T1, move to T2 (MRU).
    - If in T2, move to MRU of T2.
    '''
    _check_reset(cache_snapshot)
    key = obj.key

    if key in m_t1:
        del m_t1[key]
        m_t2[key] = 1
        m_t2.move_to_end(key)
    elif key in m_t2:
        m_t2.move_to_end(key)
    else:
        # Should be in cache, but if missing from metadata, add to T2 (self-healing)
        m_t2[key] = 1
        m_t2.move_to_end(key)

def update_after_insert(cache_snapshot, obj):
    '''
    On Cache Insert (Miss):
    - If previously in ghost B1/B2, promote to T2.
    - Otherwise, insert into T1.
    '''
    _check_reset(cache_snapshot)
    key = obj.key

    if key in m_b1:
        del m_b1[key]
        m_t2[key] = 1
        m_t2.move_to_end(key)
    elif key in m_b2:
        del m_b2[key]
        m_t2[key] = 1
        m_t2.move_to_end(key)
    else:
        # Totally new item
        m_t1[key] = 1
        m_t1.move_to_end(key)

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    On Eviction:
    - Move evicted key from T1/T2 to B1/B2.
    - Maintain capacity of ghost lists.
    '''
    key = evicted_obj.key
    capacity = cache_snapshot.capacity

    if key in m_t1:
        del m_t1[key]
        m_b1[key] = 1
        m_b1.move_to_end(key)
    elif key in m_t2:
        del m_t2[key]
        m_b2[key] = 1
        m_b2.move_to_end(key)

    # Restrict size of ghost lists to capacity
    if len(m_b1) > capacity:
        m_b1.popitem(last=False)
    if len(m_b2) > capacity:
        m_b2.popitem(last=False)
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