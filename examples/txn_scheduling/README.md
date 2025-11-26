# Transaction Scheduling Example for ShinkaEvolve

This example demonstrates how to use ShinkaEvolve to optimize transaction scheduling algorithms for minimizing makespan across multiple workload types.

## Overview

The transaction scheduling problem involves finding the optimal order to execute transactions to minimize the total makespan (completion time) across three different workload types:

1. **Workload 1**: Complex mixed read/write transactions with varying lengths
2. **Workload 2**: Simple read-then-write pattern transactions  
3. **Workload 3**: Minimal read/write operations

Each workload contains 100 transactions, and the goal is to find scheduling strategies that minimize the total makespan across all three workloads.

## Files

- `initial.py`: The initial transaction scheduling algorithm using greedy cost sampling
- `evaluate.py`: Evaluation framework that validates schedules and computes metrics
- `run_evo.py`: Evolution runner configuration for ShinkaEvolve
- `README.md`: This documentation file

## Usage

### Running the Initial Algorithm

```bash
python initial.py
```

This will run the baseline greedy cost sampling algorithm and output the total makespan and execution time.

### Evaluating a Program

```bash
python evaluate.py --program_path initial.py --results_dir results
```

This will evaluate the program, validate the schedules, and save detailed metrics to the results directory.

### Running Evolution

```bash
python run_evo.py
```

This will start the ShinkaEvolve evolution process to optimize the scheduling algorithm. Note that this requires API keys for the LLM models configured in the evolution config.

## Algorithm Details

The baseline algorithm uses a greedy cost sampling strategy:

1. **Random Starting Point**: Selects a random transaction to start the schedule
2. **Greedy Selection**: For each remaining position, samples multiple candidate transactions and selects the one that minimizes the total makespan when added to the current schedule
3. **Sampling Rate**: Uses a configurable sampling rate to balance exploration vs exploitation

## Evaluation Metrics

- **Combined Score**: Inverse of total makespan (higher is better)
- **Validity**: Ensures all schedules are valid (no duplicates, all transactions included)
- **Execution Time**: Time taken to compute the schedules
- **Individual Makespans**: Makespan for each workload

## Evolution Targets

The evolution process will explore improvements to:

- Better sampling strategies beyond random selection
- Adaptive sampling rates based on workload characteristics
- Workload-aware scheduling that adapts to transaction patterns
- Hybrid approaches combining multiple scheduling strategies
- Machine learning approaches for pattern recognition
- Dynamic parameter tuning based on workload characteristics

## Configuration

The example uses the following ShinkaEvolve configuration:

- **Task**: `txn_scheduling` (defined in `configs/task/txn_scheduling.yaml`)
- **Variant**: `txn_scheduling_example` (defined in `configs/variant/txn_scheduling_example.yaml`)
- **Evolution**: 20 generations with various LLM models
- **Database**: Island-based evolution with migration and inspiration

## Dependencies

The example depends on the transaction simulator and workload data from the `openevolve_examples/txn_scheduling` directory, which provides:

- `txn_simulator.py`: Transaction simulation and cost calculation
- `workloads.py`: Predefined workload data for testing
