# Model Placement (PRISM) Example for ShinkaEvolve

This example demonstrates how to use ShinkaEvolve to optimize GPU model placement algorithms for minimizing KV cache pressure across GPU clusters.

## Overview

The model placement problem involves assigning machine learning models to GPUs in a way that minimizes the maximum KV (key-value) cache pressure while respecting memory constraints. This is critical for serving large language models efficiently in production environments.

### Problem Definition

**Given:**
- A cluster of GPUs (each with 80 GB memory)
- A set of models to deploy, each with:
  - `model_size`: Memory footprint in GB
  - `req_rate`: Request rate (requests per second)
  - `slo`: Service level objective (latency constraint in ms)

**Constraints:**
- Each GPU has 80 GB of memory
- Sum of model sizes on each GPU must not exceed 80 GB
- All models must be successfully placed

**Objective:**
- MINIMIZE the maximum KV cache pressure (KVPR) across all GPUs

### KV Cache Pressure (KVPR)

For a GPU with assigned models M:

```
KVPR = [sum(model.req_rate / model.slo for model in M)] / (80 - sum(model.model_size for model in M))
```

Where:
- **Numerator**: Weighted request rate (higher req_rate and lower SLO = more pressure)
- **Denominator**: Available KV cache memory (memory not used by model weights)
- **Higher KVPR** = More crowded/stressed GPU

The goal is to balance the load such that no single GPU becomes a bottleneck.

## Files

- `initial.py`: The initial greedy model placement algorithm
- `evaluate.py`: Evaluation framework that validates placements and computes metrics
- `run_evo.py`: Evolution runner configuration for ShinkaEvolve
- `README.md`: This documentation file

## Usage

### Running the Initial Algorithm

```bash
cd examples/prism
python initial.py
```

This will run the baseline greedy algorithm and output the average inverse KVPR across test cases.

### Evaluating a Program

```bash
python evaluate.py --program_path initial.py --results_dir results
```

This will evaluate the program on 50 test cases, validate the placements, and save detailed metrics to the results directory.

### Running Evolution

```bash
python run_evo.py
```

This will start the ShinkaEvolve evolution process to optimize the placement algorithm. Note that this requires API keys for the LLM models configured in the evolution config.

## Algorithm Details

### Baseline Algorithm

The initial greedy algorithm works as follows:

1. **Sort Models**: Order models by `req_rate / slo` in descending order (most demanding first)
2. **Initialize State**: Track remaining memory and weighted request rate for each GPU
3. **Greedy Placement**: For each model:
   - Calculate current KVPR for each GPU if this model were added
   - Place the model on the GPU with the lowest resulting KVPR
   - Update GPU state (reduce available memory, increase weighted request rate)

### Why This Can Be Improved

The greedy approach has several limitations:
- **No look-ahead**: Doesn't consider impact on future placements
- **Simple sorting**: Only considers one metric (req_rate/slo)
- **Local decisions**: May create imbalanced configurations
- **No post-optimization**: Never revisits earlier placement decisions

## Evaluation Metrics

The evaluation runs 50 test cases with the following characteristics:
- **GPU count**: 5-10 GPUs per case
- **Model count**: 2x the number of GPUs (10-20 models)
- **Model sizes**: 10-30 GB (randomized)
- **Request rates**: 1-10 req/s
- **SLOs**: 5-10 ms

**Metrics:**
- `max_kvpr`: Average of (1 / max_kvpr) across all test cases (higher is better)
- `success_rate`: Fraction of test cases where all models were successfully placed
- `execution_time`: Average time per test case
- `combined_score`: max_kvpr + success_rate (primary optimization target)

## Optimization Strategies

The evolution process will explore various improvements:

### 1. Better Sorting Strategies
- Multi-factor sorting considering size, req_rate, and slo
- Dynamic reordering based on current GPU state
- Adaptive sorting per workload characteristics

### 2. Smarter Placement Heuristics
- **Look-ahead planning**: Consider impact on remaining models
- **Bin-packing approaches**: Treat as a multi-dimensional bin-packing problem
- **Balance multiple objectives**: Memory usage, KVPR, variance

### 3. Load Balancing
- Minimize variance across GPUs, not just the maximum
- Proactive balancing to avoid hot-spots
- Consider both current and predicted future load

### 4. Search-Based Methods
- **Local search**: Start with greedy, then improve via swaps
- **Simulated annealing**: Accept occasional worse moves to escape local optima
- **Genetic algorithms**: Evolve populations of placement solutions

### 5. Mathematical Optimization
- Integer linear programming formulations
- Constraint satisfaction approaches
- Approximation algorithms with provable bounds

## Example Improvements

Here are some concrete ideas that evolution might discover:

```python
# Multi-factor sorting
sorted_models = sorted(models, 
    key=lambda m: (m.req_rate / m.slo, m.model_size), 
    reverse=True)

# Variance-aware placement
def select_gpu(gpus, model):
    # Choose GPU that minimizes variance, not just max KVPR
    kvprs = [calculate_kvpr(gpu) for gpu in gpus]
    return min(gpus, key=lambda g: np.std(kvprs_after_adding(g, model)))

# Two-phase approach
def place_models(gpus, models):
    # Phase 1: Greedy placement
    placement = greedy_place(gpus, models)
    # Phase 2: Local optimization via swaps
    return optimize_via_swaps(placement)
```

## Dependencies

The example depends on reference files in `openevolve_examples/prism`:
- `evaluator.py`: Comprehensive evaluation logic with test case generation
- `initial_program.py`: Original OpenEvolve baseline implementation

These are automatically accessed via robust path resolution in the evaluation code.

## Configuration

The example uses the following ShinkaEvolve configuration:

- **Evolution**: 300 generations with island-based evolution
- **Islands**: 2 islands with 10% migration rate
- **Archive**: 40 best programs kept for inspiration
- **Models**: Multiple LLMs (GPT-5, Claude, Gemini) with dynamic selection
- **Patch types**: 60% diff, 30% full, 10% cross-program
- **Selection**: Weighted prioritization (λ=10.0)

## Performance Targets

Based on the baseline and problem characteristics:

- **Baseline combined_score**: ~1.0-1.5 (depends on test cases)
- **Target combined_score**: 1.5-2.0
- **Success rate**: Should maintain 100% (all models placed)
- **Execution time**: Should stay under 5s per test case

## Advanced Extensions

Once the basic placement works well, you could extend to:

- **Multi-objective optimization**: Balance KVPR, memory efficiency, and latency
- **Dynamic workloads**: Handle time-varying request rates
- **Heterogeneous GPUs**: Different GPU types with varying memory/compute
- **Model replication**: Allow same model on multiple GPUs for high demand
- **Migration costs**: Consider the cost of moving models between GPUs
- **Real-world constraints**: GPU affinity, network topology, power consumption

## Research Context

This problem is inspired by the PRISM project, which addresses model placement for large language model serving. Key insights:

- **KV cache pressure** is often the bottleneck, not model size alone
- **Request rates and SLOs** create heterogeneous load patterns
- **Greedy algorithms** provide reasonable baselines but leave room for optimization
- **Load balancing** across GPUs is critical for cluster utilization

## Troubleshooting

### Common Issues

**Memory constraint violations:**
- Ensure sum of model sizes doesn't exceed 80 GB per GPU
- Check for off-by-one errors in available memory calculations

**Timeout errors:**
- Simplify complex algorithms for faster execution
- Consider approximation vs exact solutions

**Low success rate:**
- Verify all models can theoretically fit (total size vs total memory)
- Check for edge cases with large models

**Stagnant evolution:**
- Try different parent selection strategies
- Increase temperature for more exploration
- Add more diverse inspirations

## Further Reading

- ShinkaEvolve documentation: [GitHub](https://github.com/user/ShinkaEvolve)
- Bin-packing algorithms: Classic NP-hard problem with many heuristics
- Load balancing in distributed systems: Related work on resource allocation
- LLM serving systems: PRISM, vLLM, TensorRT-LLM

This example provides a foundation for evolving sophisticated model placement algorithms that can efficiently distribute LLMs across GPU clusters.

