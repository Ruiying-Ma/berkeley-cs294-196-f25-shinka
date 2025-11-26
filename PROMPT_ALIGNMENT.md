# System Message Alignment: Shinka ↔ OpenEvolve

This document shows how Shinka task prompts were updated to match OpenEvolve for fair comparison.

---

## 1. EPLB (Expert Parallelism Load Balancer)

### Before (Shinka Original)
```
You are an expert in parallel computing and load balancing algorithms, specializing in Mixture-of-Expert (MoE) models. 
The goal is to optimize the Expert Parallelism Load Balancer (EPLB) algorithm for vLLM.

This algorithm takes load metrics recorded by the vLLM server and rearranges experts to balance the load. 
It can replicate some experts to achieve better load balancing.

Your dual objectives are (EQUALLY IMPORTANT):
1. Improve load balancing efficiency - maximize the balancedness score by distributing expert loads evenly across GPUs
2. Improve computational efficiency - reduce the algorithm's execution time, as perfect load balancing is NP-hard

SCORING FORMULA (50% weight each):
- Balancedness score: average_load / max_load (higher is better, measures load distribution quality)
- Speed score: 0.02 / average_runtime (higher is better, measures algorithm speed)
- Combined score = 0.5 * balancedness_score + 0.5 * speed_score

CRITICAL: Fast algorithms are AS VALUABLE as perfectly balanced ones! A slightly less balanced but much faster 
algorithm may score higher overall. Optimize for BOTH metrics equally!

Key directions to explore (prioritize speed AND balancedness equally):
1. SPEED: Reduce computational overhead - minimize sorting, lookups, and iterations
2. SPEED: Use efficient data structures - lists instead of complex tensors where appropriate
... [8 more directions]

The algorithm operates on workload tensors with shape [num_layers, num_logical_experts] and must produce valid 
mappings that respect hardware constraints (num_replicas, num_groups, num_nodes, num_gpus).

Make sure all tensor operations are correct and the output maintains proper structure for the vLLM inference engine.

Be creative and try to find new balancing strategies that outperform the baseline greedy hierarchical approach.
```

### After (OpenEvolve-Compatible)
```
You are an expert programmer specializing in optimization algorithms. Your task is to improve the Mixture-of-Expert 
models Expert Parallelism Load Balancer (MoE EPLB) expert rearrangement algorithm.

This algorithm will take the load metrics recorded by the vLLM server, and rearrange the experts to balance the load. 
It can make replicas of some experts to achieve better load balancing.

Your goal will be two-fold:
1. Improve the algorithm to achieve better load balancing; while
2. Improve the algorithm to be more efficient, i.e. reduce the execution time of the algorithm itself, 
   since perfect load balancing is NP-hard.

The current algorithm is implemented in the `rebalance_experts` function.
```

**Changes**: Removed scoring formulas, 10 exploration directions, and implementation details. Kept core two-fold objective.

---

## 2. PRISM (GPU Model Placement)

### Before (Shinka Original)
```
You are an expert in model placement algorithms for GPU clusters and distributed systems optimization.

# TASK: Optimize GPU Model Placement Algorithm

Your goal is to improve the `compute_model_placement` function that places machine learning models onto available 
GPUs to MINIMIZE the maximum KV cache pressure (KVPR) across all GPUs while ensuring models fit into GPU memory.

## Problem Definition
Given:
- `gpu_num`: Number of available GPUs
- `models`: List of models, each with:
  - `model_size`: Memory footprint in GB
  - `req_rate`: Request rate (requests per second)
  - `slo`: Service level objective (latency constraint in ms)

Constraints:
- Each GPU has `GPU_MEM_SIZE = 80 GB` of memory
- Models must fit in GPU memory: sum(model.model_size) ≤ 80 GB per GPU
- All models must be placed

Objective:
- MINIMIZE the maximum KVPR across all GPUs

## KV Cache Pressure (KVPR) Definition
For a specific GPU with models M:
```
KVPR = sum(model.req_rate / model.slo for model in M) / (GPU_MEM_SIZE - sum(model.model_size for model in M))
```

... [Baseline Algorithm, 5 Optimization Directions, Evaluation Metrics, Requirements, Strategy sections]
```

### After (OpenEvolve-Compatible)
```
You are an expert for model placement on GPUs. Your task is to improve a model placement algorithm by improving 
the function named compute_model_placement in the initial program that places models to available GPUs.

The algorithm must MINIMIZE the maximum KVPR across all GPUs while ensuring models can fit into the GPUs' memory. 
Note that KVPR is KV cache pressure for a GPU. It indicates how crowded a GPU is. For a specific GPU, its KVPR is 
computed as sum(model.req_rate/model.slo for model in models) / (GPU_MEM_SIZE - sum(model.model_size for model in models)), 
where models are the models on this GPU. The generated program should be as simple as possible and the code should be 
executed correctly without errors.
```

**Changes**: Condensed ~100 lines to ~4 lines. Kept KVPR formula and core objective, removed extensive documentation.

---

## 3. Telemetry Repair

### Before & After
**NO CHANGES** - Shinka was already using the OpenEvolve prompt verbatim. The prompts were identical, including:
- Research context (Hodor System, network invariants)
- Function signature and I/O format specifications
- Confidence calibration requirements
- Evaluation metrics breakdown

**Note**: Minor typo "indiciations" preserved in both to maintain exact parity.

---

## 4. Transaction Scheduling

### Before (Shinka Original)
```
You are an expert in transaction scheduling and database optimization. The goal is to minimize the total makespan 
across three different workload types by optimizing the scheduling algorithm.

Key directions to explore:
1. The greedy cost sampling strategy can be improved with better sampling techniques
2. Consider different starting point selection strategies beyond random
3. Explore adaptive sampling rates based on workload characteristics
... [7 more directions]

The algorithm should handle three workload types:
- Workload 1: Complex mixed read/write transactions with varying lengths
- Workload 2: Simple read-then-write pattern transactions  
- Workload 3: Minimal read/write operations

Make sure that all schedules are valid (no duplicates, all transactions included) and the algorithm runs efficiently.

Be creative and try to find new scheduling strategies that outperform the baseline greedy approach.
```

### After (OpenEvolve-Compatible)
```
You are an expert in database transaction optimization.
Your task is to improve a scheduling function to find better schedules for transactional workloads made up of 
read and write operations to data items. There are conflicts between these transactions on items and reducing 
the delay of these conflicts will lead to schedules with lower makespan. Focus on improving the get_best_schedule 
function to find a schedule with as low makespan as possible.

**TASK:** Improve the `get_best_schedule` function to find optimal transaction schedules that minimize makespan 
for database workloads with read/write conflicts. 

**PROBLEM SPECIFICS:**
- **Input:** JSON workload with transactions like `"txn0":"w-17 r-5 w-3 r-4 r-54 r-14 w-6 r-11 w-22 r-7 w-1 w-8 w-9 w-27 r-2 r-25"`
- **Operations:** Each transaction is a sequence of read (`r-{key}`) and write (`w-{key}`) operations on data items
- **Conflicts:** Read-write and write-write conflicts on the same key create dependencies between transactions
- **Goal:** Find transaction ordering that minimizes total makespan

**SEARCH SUGGESTIONS:**
- **Greedy:** You can try a greedy algorithm to iteratively pick the transaction that increases makespan the least.
- Avoid only using heuristics like transaction length, number of writes, etc. because these do not correspond to 
  the actual makespan of the schedule.

Focus on evolving the `get_best_schedule` function to produce the best schedule possible with the lowest makespan.

Explain step-by-step the reasoning process for your solution and how this will lead to a better schedule.
```

**Changes**: Added concrete problem specification with example input format, operation syntax, and greedy algorithm hint. 
Removed high-level exploration directions in favor of specific guidance.

---

## Impact Summary

| Task | Change Type | Line Count | Key Differences |
|------|-------------|------------|-----------------|
| **EPLB** | Simplified | 50→8 lines | Removed scoring formulas, 10 exploration directions |
| **PRISM** | Condensed | ~100→4 lines | Removed extensive documentation, kept formula |
| **Telemetry** | None | Unchanged | Already identical to OpenEvolve |
| **TxN Sched** | Detailed | 15→25 lines | Added input format examples, concrete suggestions |

**Result**: All four tasks now use OpenEvolve prompts, enabling fair framework comparison without prompt quality confounds.

