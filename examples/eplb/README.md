# Expert Parallelism Load Balancer (EPLB) Example for ShinkaEvolve

This example demonstrates how to use ShinkaEvolve to optimize the Expert Parallelism Load Balancer (EPLB) algorithm for vLLM Mixture-of-Expert models.

## Overview

The EPLB algorithm rearranges expert assignments in MoE models to balance computational load across GPUs. The algorithm must:

1. **Maximize load balancing** - Distribute expert workloads evenly across physical GPUs
2. **Minimize execution time** - Run efficiently since perfect load balancing is NP-hard

The algorithm takes workload statistics `[num_layers, num_logical_experts]` and produces mappings that respect hardware constraints:
- `num_replicas`: Total number of physical expert replicas
- `num_groups`: Number of expert groups
- `num_nodes`: Number of server nodes (with fast intra-node connections like NVLink)
- `num_gpus`: Total number of GPUs

## Files

- `initial.py`: Initial EPLB algorithm implementation with hierarchical balancing
- `evaluate.py`: Evaluation framework that validates mappings and computes balancedness scores
- `run_evo.py`: Evolution runner configuration for ShinkaEvolve
- `README.md`: This documentation file

## Setup

### Install Dependencies

```bash
# Install PyTorch (required for tensor operations)
pip install torch

# Install ShinkaEvolve (if not already installed)
pip install -e .
```

### Download Workload Data

The evaluator needs real workload statistics from vLLM. Download the workload file:

```bash
cd /Users/audreycc/Documents/Work/LLMTxn/ShinkaEvolve/openevolve_examples/eplb
wget https://huggingface.co/datasets/abmfy/eplb-openevolve/resolve/main/expert-load.json
```

This file contains historical expert load data used for evaluation.

## Usage

### Running the Initial Algorithm

```bash
python initial.py
```

This will import the baseline EPLB implementation.

### Evaluating a Program

```bash
python evaluate.py --program_path initial.py --results_dir results
```

This will:
1. Load workload history from `expert-load.json`
2. Run the EPLB algorithm on each workload sample
3. Simulate inference with the generated expert mappings
4. Compute balancedness and speed scores
5. Save detailed metrics to the results directory

### Running Evolution

```bash
# Using the CLI (recommended)
cd /Users/audreycc/Documents/Work/LLMTxn/ShinkaEvolve
shinka_launch variant=eplb_example

# Or using Python API
cd examples/eplb
python run_evo.py
```

This will start the ShinkaEvolve evolution process to optimize the EPLB algorithm.

## Algorithm Details

The baseline EPLB algorithm uses a hierarchical approach:

1. **Group Packing**: Pack expert groups to server nodes using balanced packing
2. **Expert Replication**: Create expert replicas within nodes to balance load
3. **GPU Placement**: Distribute physical experts to GPUs with balanced packing

Key functions:
- `balanced_packing()`: Bin-packing algorithm that assigns items to bins while balancing weights
- `replicate_experts()`: Creates expert replicas to minimize maximum load
- `rebalance_experts_hierarchical()`: Hierarchical load balancing across nodes and GPUs
- `rebalance_experts()`: Main entry point that selects appropriate balancing strategy

## Evaluation Metrics

- **Balancedness Score**: Ratio of average load to maximum load across GPUs (higher is better, max 1.0)
- **Speed Score**: Inverse of algorithm execution time (higher is faster)
- **Combined Score**: Average of balancedness and speed scores

The evaluation simulates vLLM inference with the generated expert mappings on future workload samples (predictive balancing).

## Evolution Targets

The evolution process will explore improvements to:

- Better bin-packing heuristics in `balanced_packing`
- Smarter replication strategies in `replicate_experts`
- Alternative grouping and hierarchical approaches
- Approximation algorithms that trade optimality for speed
- GPU topology-aware placement strategies
- Look-ahead strategies instead of greedy selection
- Dynamic programming and memoization techniques
- Vectorization and parallel processing opportunities
- Machine learning approaches for expert placement prediction

## Configuration

The example uses the following ShinkaEvolve configuration:

- **Task**: `eplb` (defined in `configs/task/eplb.yaml`)
- **Variant**: `eplb_example` (defined in `configs/variant/eplb_example.yaml`)
- **Evolution**: 400 generations with various LLM models
- **Database**: Island-based evolution with migration and inspiration

## Hardware Configuration

Default EPLB configuration (matching DeepSeek's setup):
- 288 physical expert replicas
- 8 expert groups
- 4 server nodes
- 32 GPUs total

These can be modified in the evaluator to match your target hardware.

## References

- [DeepSeek EPLB](https://github.com/deepseek-ai/eplb) - Original EPLB implementation
- [vLLM](https://github.com/vllm-project/vllm) - Fast LLM inference engine
- [EPLB Issue #12](https://github.com/deepseek-ai/EPLB/issues/12) - Algorithm walkthrough example

