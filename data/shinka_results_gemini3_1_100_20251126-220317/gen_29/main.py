# EVOLVE-BLOCK-START
"""
W-TinyLFU (Window Tiny Least Frequently Used) Implementation
- Window LRU (5%): Filters one-hit wonders (scans).
- Main SLRU (95%): Protected (80%) + Probation (20%).
- TinyLFU Admission: Window victims duel with Probation victims based on frequency.
- Frequency: Approximate counting with periodic aging (halving).
"""
from collections import OrderedDict

# Global State Dictionary
gs = {
    'w_lru': OrderedDict(),     # Window Segment
    'p_lru': OrderedDict(),     # Probation Segment (Main)
    'm_lru': OrderedDict(),     # Protected Segment (Main)
    'freq': {},                 # Frequency Counter
    'doorkeeper': set(),        # 1-bit filter
    'access_count': 0           # Internal counter for aging
}

def evict(cache_snapshot, obj):
    g = gs
    w = g['w_lru']
    p = g['p_lru']
    m = g['m_lru']
    
    # Configuration
    cap = cache_snapshot.capacity
    w_cap = max(1, int(cap * 0.05)) # 5% Window
    
    # 1. Check Window Overflow -> Dueling
    if len(w) > w_cap:
        # If Main is empty, we must evict from Window
        if not p and not m:
            return next(iter(w))
        
        # Ensure we have a candidate from Probation
        if not p:
            # If Probation is empty but Protected isn't, demote LRU from Protected
            # to populate Probation for the duel, or just evict from Window.
            # Standard W-TinyLFU would evict from Window if Main is fully Protected 
            # and we don't want to demote yet, but let's try to find a victim in Main.
            if m:
                # Force demotion to find a victim in Main
                k, _ = m.popitem(last=False)
                p[k] = None
            else:
                 return next(iter(w))

        # Candidate Selection
        cand_w = next(iter(w)) # Window LRU
        cand_p = next(iter(p)) # Probation LRU
        
        # Frequency Estimate
        fw = g['freq'].get(cand_w, 0) + (1 if cand_w in g['doorkeeper'] else 0)
        fp = g['freq'].get(cand_p, 0) + (1 if cand_p in g['doorkeeper'] else 0)
        
        # Duel
        if fw > fp:
            # Window Wins: Promote W to Probation, Evict P
            del w[cand_w]
            p[cand_w] = None
            return cand_p
        else:
            # Main Wins (or tie/scan): Evict W
            return cand_w

    # 2. Window not full -> Evict from Main (to make space for new Window inserts)
    # This happens when the total cache is full, but Window is below its specific cap.
    if p:
        return next(iter(p))
    elif m:
        return next(iter(m))
    elif w:
        return next(iter(w))
        
    # Failsafe
    return next(iter(cache_snapshot.cache))

def update_after_hit(cache_snapshot, obj):
    g = gs
    key = obj.key
    _update_freq(g, key, cache_snapshot.capacity)
    
    if key in g['m_lru']:
        g['m_lru'].move_to_end(key)
    elif key in g['p_lru']:
        # Promote: Probation -> Protected
        del g['p_lru'][key]
        g['m_lru'][key] = None
        _balance_main(g, cache_snapshot.capacity)
    elif key in g['w_lru']:
        # Window Hit: Move to MRU of Window
        g['w_lru'].move_to_end(key)

def update_after_insert(cache_snapshot, obj):
    g = gs
    # Detect Trace Reset
    if cache_snapshot.access_count <= 1:
        g['w_lru'].clear()
        g['p_lru'].clear()
        g['m_lru'].clear()
        g['freq'].clear()
        g['doorkeeper'].clear()
        g['access_count'] = 0

    key = obj.key
    _update_freq(g, key, cache_snapshot.capacity)
    
    # Insert new items into Window
    g['w_lru'][key] = None

def update_after_evict(cache_snapshot, obj, evicted_obj):
    g = gs
    key = evicted_obj.key
    
    if key in g['w_lru']: del g['w_lru'][key]
    elif key in g['p_lru']: del g['p_lru'][key]
    elif key in g['m_lru']: del g['m_lru'][key]

def _update_freq(g, key, capacity):
    g['access_count'] += 1
    
    # Doorkeeper Logic
    if key in g['doorkeeper']:
        g['freq'][key] = g['freq'].get(key, 0) + 1
    else:
        g['doorkeeper'].add(key)
        
    # Periodic Aging (Reset)
    if g['access_count'] >= capacity * 10:
        _age_frequency(g)
        g['access_count'] = 0
        
    # Safety Pruning to prevent Memory Bloat
    if len(g['freq']) > capacity * 5:
        _prune_frequency(g)

def _age_frequency(g):
    g['doorkeeper'].clear()
    rem = []
    for k in g['freq']:
        g['freq'][k] //= 2
        if g['freq'][k] == 0:
            rem.append(k)
    for k in rem:
        del g['freq'][k]

def _prune_frequency(g):
    # Remove low frequency items to save memory
    rem = [k for k, v in g['freq'].items() if v <= 2]
    for k in rem:
        del g['freq'][k]

def _balance_main(g, capacity):
    # Enforce Protected Segment Size (80% of Main)
    w_cap = max(1, int(capacity * 0.05))
    main_cap = max(1, capacity - w_cap)
    protected_cap = int(main_cap * 0.8)
    
    while len(g['m_lru']) > protected_cap:
        # Demote LRU of Protected to Probation
        k, _ = g['m_lru'].popitem(last=False)
        g['p_lru'][k] = None
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