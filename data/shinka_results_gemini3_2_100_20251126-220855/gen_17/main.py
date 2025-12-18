# EVOLVE-BLOCK-START
from collections import OrderedDict

# LIRS Refined State
# m_s: Stack S. Keys -> None. Order: Recency (MRU right).
#      Contains LIR blocks and HIR blocks (both resident and non-resident).
# m_q: Queue Q (Resident HIR). Keys -> None. Order: Recency (MRU right).
#      Contains only Resident HIR blocks.
# m_lir: Set of keys that are currently LIR status.
# m_lirs_reset_counter: To detect trace changes and reset state.
m_s = OrderedDict()
m_q = OrderedDict()
m_lir = set()
m_lirs_reset_counter = 0

def check_reset(cache_snapshot):
    '''
    Resets internal state if a new trace is detected (time moved backwards).
    '''
    global m_lirs_reset_counter, m_s, m_q, m_lir
    if cache_snapshot.access_count < m_lirs_reset_counter:
        m_s.clear()
        m_q.clear()
        m_lir.clear()
    m_lirs_reset_counter = cache_snapshot.access_count

def prune_stack():
    '''
    Remove HIR blocks from the bottom of the stack S.
    The LIRS algorithm requires the bottom of S to be a LIR block.
    We pop HIRs until a LIR is found or S is empty.
    '''
    global m_s, m_lir
    while m_s:
        k = next(iter(m_s))
        if k not in m_lir:
            m_s.popitem(last=False)
        else:
            break

def evict(cache_snapshot, obj):
    '''
    LIRS Eviction Strategy:
    1. Prefer evicting a Resident HIR block (from Q).
    2. If no Resident HIRs, evict a LIR block (from bottom of S).
    '''
    check_reset(cache_snapshot)
    global m_s, m_q, m_lir

    # Ensure stack invariant: Bottom of S must be LIR
    prune_stack()

    # 1. Evict from Q (Resident HIR) if available
    if m_q:
        return next(iter(m_q))
    
    # 2. Evict from LIR (Bottom of S)
    # Since we pruned, bottom of S is a LIR.
    if m_s:
        return next(iter(m_s))
        
    # Fallback (should not happen if cache is not empty)
    return None

def update_after_hit(cache_snapshot, obj):
    '''
    Handle Hits:
    - LIR hit: Update recency, prune stack.
    - HIR hit (Resident): Promote to LIR if in Stack, else update recency in Q.
    - HIR hit (Non-Resident/Ghost): Treated as Insert logic usually, but here handled if obj in S.
    '''
    check_reset(cache_snapshot)
    global m_s, m_q, m_lir
    key = obj.key
    capacity = cache_snapshot.capacity
    # Maintain a small HIR segment (1%) to allow new blocks to compete
    hir_limit = max(1, int(capacity * 0.01))
    lir_limit = capacity - hir_limit
    
    if key in m_lir:
        # LIR Hit
        # Move to MRU in S
        if key in m_s:
            m_s.move_to_end(key)
        else:
            # Restoration if needed
            m_s[key] = None
        # Prune if the moved item was at bottom (exposing HIRs)
        prune_stack()
        
    elif key in m_q:
        # Resident HIR Hit
        if key in m_s:
            # Hot HIR (In Stack) -> Promote to LIR
            m_lir.add(key)
            del m_q[key]
            m_s.move_to_end(key)
            
            # Demote a LIR to maintain limit if needed
            if len(m_lir) > lir_limit:
                prune_stack() # Ensure bottom is LIR to pick correct victim
                if m_s:
                    demoted = next(iter(m_s))
                    if demoted in m_lir:
                        m_lir.remove(demoted)
                        m_q[demoted] = None # Move to Q
                        m_s.popitem(last=False) # Remove from S
                        prune_stack()
        else:
            # Cold HIR (Not in Stack) -> Stay HIR
            # Move to MRU of Q, and bring back to top of S
            m_q.move_to_end(key)
            m_s[key] = None

    else:
        # Item in cache but not in Q or LIR (Should be rare/sync issue)
        # Treat as new Resident HIR
        if key not in m_q:
            m_q[key] = None
            m_s[key] = None

def update_after_insert(cache_snapshot, obj):
    '''
    Handle Miss (Insert):
    - If in S (Non-Resident HIR): Promote to LIR (successful test).
    - Else (New): Insert as HIR.
    '''
    check_reset(cache_snapshot)
    global m_s, m_q, m_lir
    key = obj.key
    capacity = cache_snapshot.capacity
    hir_limit = max(1, int(capacity * 0.01))
    lir_limit = capacity - hir_limit

    if key in m_s:
        # Ghost Hit (Non-Resident HIR in Stack)
        # Promote to LIR
        m_lir.add(key)
        m_s.move_to_end(key)
        
        # Demote LIR to make space in LIR set
        if len(m_lir) > lir_limit:
            prune_stack()
            if m_s:
                demoted = next(iter(m_s))
                if demoted in m_lir:
                    m_lir.remove(demoted)
                    m_q[demoted] = None
                    m_s.popitem(last=False)
                    prune_stack()
    else:
        # Cold Miss / New item -> Insert as HIR
        m_q[key] = None
        m_s[key] = None

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    Handle Eviction:
    - Remove from Resident sets (Q or LIR).
    - Key remains in S as Non-Resident HIR (Ghost) if it was there.
    '''
    check_reset(cache_snapshot)
    global m_s, m_q, m_lir
    key = evicted_obj.key
    
    if key in m_q:
        del m_q[key]
    if key in m_lir:
        m_lir.remove(key)
        # If we evicted a LIR, it was likely the bottom of S, so prune.
        prune_stack()
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