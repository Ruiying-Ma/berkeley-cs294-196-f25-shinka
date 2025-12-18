# EVOLVE-BLOCK-START
"""Hybrid SLRU + TinyLFU with ARC-style ghost feedback and scan protection.

Structural redesign:
- Policy encapsulated in HybridSLRUTinyLFU class.
- Resident segments: P0 (probation), P1 (warm), H (hot).
- Ghost segments: G0, G1, GH to capture recent evictions from respective segments.
- Adaptive parameter p controls target size of probation P0 (like ARC).
- Hot-share h_share splits protected space between P1 and H.
- TinyLFU-like lazy aging frequency estimator guides promotions and victim choice.
- Scan detector based on EWMAs; while active, require two touches to promote from P0.

Interface:
- evict chooses victim using adaptive REPLACE with small sampling by (freq asc, age asc).
- update_after_hit updates frequency and promotes across segments.
- update_after_insert performs ARC-style p update on ghost hits and places item.
- update_after_evict moves to appropriate ghost and trims.

Notes:
- Keys are strings; cache capacity is treated as object count.
- OrderedDict used for O(1) LRU/MRU operations.
"""

from collections import OrderedDict, deque

class HybridSLRUTinyLFU:
    def __init__(self):
        # Resident segments (LRU at left, MRU at right)
        self.P0 = OrderedDict()  # probation: new or low confidence
        self.P1 = OrderedDict()  # warm: reused, not yet hot
        self.H  = OrderedDict()  # hot: multi-reused

        # Ghost segments: remember evicted keys (with eviction time)
        self.G0 = OrderedDict()
        self.G1 = OrderedDict()
        self.GH = OrderedDict()
        self.ghost_ts = {}  # key -> eviction access_count

        # Adaptive parameter: target size for P0 (0..c)
        self.p = 0.0
        self.p_mom = 0.0  # momentum for smoother updates
        self.p_cooldown = 0  # small cooldown between p updates

        # Share of protected space (P1+H) given to H (0..1)
        self.h_share = 0.6

        # Lazy TinyLFU frequency estimator
        self.freq = {}       # key -> count (saturating small int)
        self.freq_epoch = {} # key -> last epoch at which count was materialized
        self.epoch = 0
        self.last_epoch_c = 0

        # Light metadata
        self.ts = {}            # last access timestamp per key
        self.touch_once = set() # used in scan_mode: tracks P0 keys that have one touch

        # Scan detection via EWMAs and small recent set
        self.ewma_hit = 0.0
        self.ewma_unique = 0.0
        self.scan_mode_ticks = 0
        self.scan_adjust_counter = 0
        self.recent_seen = set()
        self.recent_queue = deque()
        self.last_capacity = None

    # ---------- Capacity and sync ----------
    def _capacity(self, cache_snapshot):
        cap = cache_snapshot.capacity or max(len(cache_snapshot.cache), 1)
        return max(int(cap), 1)

    def _current_epoch(self, cache_snapshot):
        c = max(self._capacity(cache_snapshot), 1)
        # One epoch per capacity accesses
        return cache_snapshot.access_count // c

    def _init_or_update(self, cache_snapshot):
        c = self._capacity(cache_snapshot)
        if self.last_capacity != c:
            # Clamp p and trim ghosts on capacity change
            self.p = max(0.0, min(self.p, float(c)))
            self._trim_ghosts_to(2 * c)
            self.last_capacity = c
        # Update epoch
        self.epoch = self._current_epoch(cache_snapshot)
        # Sync resident lists with actual cache keys
        self._sync_with_cache(cache_snapshot)

    def _sync_with_cache(self, cache_snapshot):
        keys_in_cache = set(cache_snapshot.cache.keys())
        # Remove missing from resident segments
        for seg in (self.P0, self.P1, self.H):
            rem = [k for k in seg.keys() if k not in keys_in_cache]
            for k in rem:
                seg.pop(k, None)
                self.touch_once.discard(k)
        # Cleanup old freq metadata for completely unknown keys (not in any seg or ghost)
        known = keys_in_cache | set(self.G0.keys()) | set(self.G1.keys()) | set(self.GH.keys())
        for k in list(self.freq.keys()):
            if k not in known:
                self.freq.pop(k, None)
                self.freq_epoch.pop(k, None)
                self.ts.pop(k, None)

    def _trim_ghosts_to(self, max_total):
        # Keep total tracked (resident + ghost) within bound by trimming oldest ghosts
        while (len(self.P0) + len(self.P1) + len(self.H) + len(self.G0) + len(self.G1) + len(self.GH)) > max_total:
            # Trim prefer G0, then G1, then GH
            if self.G0:
                k, _ = self.G0.popitem(last=False)
                self.ghost_ts.pop(k, None)
            elif self.G1:
                k, _ = self.G1.popitem(last=False)
                self.ghost_ts.pop(k, None)
            elif self.GH:
                k, _ = self.GH.popitem(last=False)
                self.ghost_ts.pop(k, None)
            else:
                break

    # ---------- Frequency (TinyLFU-like lazy aging) ----------
    def _aged_count(self, key):
        # Return aged frequency count and refresh to current epoch lazily
        cnt = self.freq.get(key, 0)
        k_ep = self.freq_epoch.get(key, self.epoch)
        delta = self.epoch - k_ep
        if delta > 0 and cnt:
            cnt = max(cnt >> delta, 0)
            self.freq[key] = cnt
            self.freq_epoch[key] = self.epoch
        elif key not in self.freq_epoch:
            self.freq_epoch[key] = self.epoch
        return cnt

    def _record_access(self, cache_snapshot, key):
        # TinyLFU update with lazy aging
        _ = self._aged_count(key)
        self.freq[key] = min(self.freq.get(key, 0) + 1, 255)  # cap at 255 (1 byte)
        self.freq_epoch[key] = self.epoch
        self.ts[key] = cache_snapshot.access_count

    # ---------- Scan detection ----------
    def _scan_tick(self, cache_snapshot, is_hit, is_unique_insert):
        c = self._capacity(cache_snapshot)
        alpha = 1.0 / max(c, 1)
        # EWMA updates
        self.ewma_hit = (1 - alpha) * self.ewma_hit + alpha * (1.0 if is_hit else 0.0)
        self.ewma_unique = (1 - alpha) * self.ewma_unique + alpha * (1.0 if is_unique_insert else 0.0)

        # Activate scan mode if unique inserts dominate and hits are low
        if self.scan_mode_ticks <= 0 and self.ewma_unique > 0.7 and self.ewma_hit < 0.15:
            self.scan_mode_ticks = c  # stay for next window
            self.scan_adjust_counter = 0

        # If in scan mode, slowly reduce p to bias against P0 growth
        if self.scan_mode_ticks > 0:
            self.scan_mode_ticks -= 1
            self.scan_adjust_counter += 1
            if self.scan_adjust_counter % 50 == 0:
                # Heuristic decrease magnitude based on ghost pressure
                step = 1.5 * max(1.0, len(self.G0) / float(max(1, len(self.G1) + len(self.GH))))
                self.p = max(0.0, self.p - step)
                self.p_mom *= 0.5  # damp momentum while in scan mode

        # Maintain bounded recent set for uniqueness approximation
        # Add recent keys in insert path where we know the key; here we don't know key, handled in update_after_insert

    # ---------- Replacement helpers ----------
    def _replace_side(self, incoming_key, c):
        # ARC-style decision with ghosts: prefer evicting from P0 if it exceeds target p
        len_p0 = len(self.P0)
        p_int = int(round(self.p))
        if len_p0 > p_int or (len_p0 == p_int and (incoming_key in self.G1 or incoming_key in self.GH)):
            return 'P0'
        # Otherwise evict from protected space; bias to P1 first, then H
        return 'PROT'

    def _sample_victim(self, od: OrderedDict, sample_k=2):
        # Among first sample_k LRU candidates, pick min by (freq asc, age asc)
        if not od:
            return None
        it = iter(od.keys())
        candidates = []
        for _ in range(sample_k):
            try:
                k = next(it)
            except StopIteration:
                break
            candidates.append(k)
        if not candidates:
            return None
        # Score: lower aged frequency first, then older timestamp
        def score(k):
            return (self._aged_count(k), self.ts.get(k, -1))
        # We want min freq and min ts (older), so combine by (freq, ts)
        return min(candidates, key=lambda k: (score(k)[0], score(k)[1]))

    # ---------- Public policy interface ----------
    def evict(self, cache_snapshot, obj):
        self._init_or_update(cache_snapshot)
        c = self._capacity(cache_snapshot)
        keys_in_cache = set(cache_snapshot.cache.keys())

        # Fallback if nothing tracked
        if not keys_in_cache:
            return None
        if not (self.P0 or self.P1 or self.H):
            # Evict globally oldest by timestamp if available
            if self.ts:
                return min(keys_in_cache, key=lambda k: self.ts.get(k, -1))
            return next(iter(keys_in_cache))

        side = self._replace_side(obj.key, c)

        # Adaptive sampling sizes (pressure-aware)
        protected = len(self.P1) + len(self.H)
        t1_sample = 1 if (len(self.P0) > int(round(self.p)) + max(int(0.1 * c), 1) or self.scan_mode_ticks > 0) else 2
        # Prefer more careful selection in protected when it's large or promotions are frequent (approx via H size)
        tprot_sample = 5 if (protected > max(c - int(round(self.p)), 0) or len(self.H) > int(0.4 * max(protected, 1))) else 3
        if self.ewma_hit < 0.2:
            tprot_sample = max(2, tprot_sample - 1)

        victim = None
        if side == 'P0' and self.P0:
            victim = self._sample_victim(self.P0, t1_sample) or next(iter(self.P0))
        else:
            # Evict from protected: bias P1, fallback to H
            if self.P1:
                victim = self._sample_victim(self.P1, tprot_sample) or next(iter(self.P1))
            elif self.H:
                victim = self._sample_victim(self.H, tprot_sample) or next(iter(self.H))
            else:
                # Edge: protected empty, fall back to P0
                if self.P0:
                    victim = self._sample_victim(self.P0, t1_sample) or next(iter(self.P0))

        # Defensive: ensure victim is in actual cache
        if victim not in keys_in_cache:
            # Try others
            for seg in (self.P0, self.P1, self.H):
                for k in seg.keys():
                    if k in keys_in_cache:
                        victim = k
                        break
                if victim in keys_in_cache:
                    break
            else:
                # Fallback: oldest known
                if self.ts:
                    return min(keys_in_cache, key=lambda k: self.ts.get(k, -1))
                return next(iter(keys_in_cache))
        return victim

    def update_after_hit(self, cache_snapshot, obj):
        self._init_or_update(cache_snapshot)
        key = obj.key
        self._record_access(cache_snapshot, key)
        self._scan_tick(cache_snapshot, is_hit=True, is_unique_insert=False)

        # Promotion policy
        if key in self.P0:
            if self.scan_mode_ticks > 0:
                # Require two touches: first touch stays in P0 MRU
                if key in self.touch_once:
                    self.touch_once.discard(key)
                    # Promote to P1
                    self.P0.pop(key, None)
                    self.P1[key] = True
                else:
                    self.touch_once.add(key)
                    # Refresh recency
                    self.P0.move_to_end(key, last=True)
            else:
                # Normal: promote on first hit
                self.P0.pop(key, None)
                self.P1[key] = True
        elif key in self.P1:
            # If frequency is high enough, promote to H; else refresh
            if self._aged_count(key) >= 2:
                self.P1.pop(key, None)
                self.H[key] = True
            else:
                self.P1.move_to_end(key, last=True)
        elif key in self.H:
            # Refresh recency
            self.H.move_to_end(key, last=True)
        else:
            # If metadata lost, reinsert as protected warm
            self.P1[key] = True

        # Maintain rough sizing by demoting oldest from H to P1 if H dominates
        c = self._capacity(cache_snapshot)
        protected_target = max(c - int(round(self.p)), 0)
        h_cap = int(self.h_share * protected_target)
        if len(self.H) > h_cap and self.H:
            demote = next(iter(self.H))
            if demote:
                self.H.pop(demote, None)
                self.P1[demote] = True

    def update_after_insert(self, cache_snapshot, obj):
        self._init_or_update(cache_snapshot)
        key = obj.key
        self._record_access(cache_snapshot, key)

        # Uniqueness tracking for scan EWMA
        c = self._capacity(cache_snapshot)
        is_unique = key not in self.recent_seen and key not in self.P0 and key not in self.P1 and key not in self.H \
                    and key not in self.G0 and key not in self.G1 and key not in self.GH
        # Update recent set
        if key not in self.recent_seen:
            self.recent_seen.add(key)
            self.recent_queue.append(key)
            # Bound recent set
            bound = 2 * c
            while len(self.recent_queue) > bound:
                old = self.recent_queue.popleft()
                if old in self.recent_seen and old not in (self.P0 or {}) and old not in (self.P1 or {}) and old not in (self.H or {}):
                    # Keep it simple: always remove from set when popped
                    self.recent_seen.discard(old)
        self._scan_tick(cache_snapshot, is_hit=False, is_unique_insert=is_unique)

        now = cache_snapshot.access_count

        # ARC-style adaptation with freshness-aware delta and momentum
        def _freshness_weight(k):
            ev_t = self.ghost_ts.get(k, None)
            if ev_t is None:
                return 1.0
            age = max(0, now - ev_t)
            return 1.5 if age <= (0.5 * c) else 1.0

        updated_p = False
        if key in self.G0:
            delta = max(1.0, (len(self.G1) + len(self.GH)) / float(max(1, len(self.G0))))
            step = delta * _freshness_weight(key)
            sign = +1.0
            if self.p_cooldown <= 0:
                self.p_mom = 0.5 * self.p_mom + sign * min(step, 0.25 * c)
                self.p = max(0.0, min(float(c), self.p + self.p_mom))
                self.p_cooldown = 10
            updated_p = True
            # Admit into protected warm
            self.G0.pop(key, None)
            self.P1[key] = True
        elif key in self.G1 or key in self.GH:
            delta = max(1.0, len(self.G0) / float(max(1, len(self.G1) + len(self.GH))))
            step = delta * _freshness_weight(key)
            sign = -1.0
            if self.p_cooldown <= 0:
                self.p_mom = 0.5 * self.p_mom + sign * min(step, 0.25 * c)
                self.p = max(0.0, min(float(c), self.p + self.p_mom))
                self.p_cooldown = 10
            updated_p = True
            # Admit into hot if quite fresh or high freq, else warm
            if _freshness_weight(key) > 1.0 or self._aged_count(key) >= 3:
                self.G1.pop(key, None)
                self.GH.pop(key, None)
                self.H[key] = True
            else:
                self.G1.pop(key, None)
                self.GH.pop(key, None)
                self.P1[key] = True
        else:
            # Brand new: insert into probation
            self.P0[key] = True
            # If in scan mode, mark as untouched
            self.touch_once.discard(key)

        if self.p_cooldown > 0:
            self.p_cooldown -= 1

        # Enforce soft target splits: if protected overflows vs capacity, demote from warm to ghosts
        total_res = len(self.P0) + len(self.P1) + len(self.H)
        # Defensive: if total exceeds capacity (metadata lag), move oldest from the most overfull side to ghosts
        if total_res > c:
            side = 'P0' if len(self.P0) > int(round(self.p)) else 'PROT'
            if side == 'P0' and self.P0:
                k = next(iter(self.P0))
                self.P0.pop(k, None)
                self.G0[k] = True
                self.ghost_ts[k] = now
            else:
                # Prefer demote from P1, else from H
                if self.P1:
                    k = next(iter(self.P1))
                    self.P1.pop(k, None)
                    self.G1[k] = True
                    self.ghost_ts[k] = now
                elif self.H:
                    k = next(iter(self.H))
                    self.H.pop(k, None)
                    self.GH[k] = True
                    self.ghost_ts[k] = now

        # Bound H size within protected target
        protected_target = max(c - int(round(self.p)), 0)
        h_cap = int(self.h_share * protected_target)
        while len(self.H) > max(h_cap, 0) and self.H:
            demote = next(iter(self.H))
            self.H.pop(demote, None)
            self.P1[demote] = True

        # Trim ghosts
        self._trim_ghosts_to(2 * c)

    def update_after_evict(self, cache_snapshot, obj, evicted_obj):
        self._init_or_update(cache_snapshot)
        evk = evicted_obj.key
        now = cache_snapshot.access_count

        # Move evicted resident to appropriate ghost
        if evk in self.P0:
            self.P0.pop(evk, None)
            self.G0[evk] = True
            self.ghost_ts[evk] = now
            self.touch_once.discard(evk)
        elif evk in self.P1:
            self.P1.pop(evk, None)
            self.G1[evk] = True
            self.ghost_ts[evk] = now
        elif evk in self.H:
            self.H.pop(evk, None)
            self.GH[evk] = True
            self.ghost_ts[evk] = now
        else:
            # If metadata missing, avoid growing ghosts indefinitely
            self.G0.pop(evk, None)
            self.G1.pop(evk, None)
            self.GH.pop(evk, None)
            self.ghost_ts.pop(evk, None)

        # Drop frequency for evicted to avoid stale bias
        self.freq.pop(evk, None)
        self.freq_epoch.pop(evk, None)
        self.ts.pop(evk, None)

        # Trim ghosts within bound
        c = self._capacity(cache_snapshot)
        self._trim_ghosts_to(2 * c)


# Global policy instance
_policy = HybridSLRUTinyLFU()

def evict(cache_snapshot, obj):
    '''
    Return the key of the cached object that will be evicted to make room for `obj`.
    '''
    return _policy.evict(cache_snapshot, obj)

def update_after_hit(cache_snapshot, obj):
    '''
    Update metadata immediately after a cache hit.
    '''
    _policy.update_after_hit(cache_snapshot, obj)

def update_after_insert(cache_snapshot, obj):
    '''
    Update metadata immediately after inserting a new object into the cache.
    '''
    _policy.update_after_insert(cache_snapshot, obj)

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    Update metadata immediately after evicting the victim.
    '''
    _policy.update_after_evict(cache_snapshot, obj, evicted_obj)
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