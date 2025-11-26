# Network Telemetry Repair Example for ShinkaEvolve

This example demonstrates how to use ShinkaEvolve to evolve algorithms for validating and repairing network interface telemetry data.

## Overview

Network controllers often receive incorrect telemetry that doesn't accurately reflect the true network state, leading to major outages. This example tackles the challenge of detecting and repairing corrupted network measurements by exploiting inherent relationships in network topology.

### Problem Description

Network telemetry data can become corrupted due to:
- Hardware failures
- Measurement errors  
- Configuration mistakes
- Timing issues between measurements

The goal is to validate and repair this data using network invariants:
1. **Link Symmetry (R3)**: TX rate on one interface should match RX rate on connected interface
2. **Flow Conservation (R1)**: Incoming traffic should equal outgoing traffic at each router
3. **Interface Consistency**: Status should be consistent across connected pairs

## Files

- `initial.py`: Initial repair algorithm using basic link symmetry checks
- `evaluate.py`: Shinka evaluation wrapper that tests repairs against ground truth
- `run_evo.py`: Local evolution runner configuration
- `README.md`: This documentation file

## Data Format

### Input Telemetry
```python
telemetry = {
    'if1_to_if2': {
        'interface_status': 'up',      # 'up' or 'down'
        'rx_rate': 100.0,              # Receive rate in Mbps
        'tx_rate': 95.0,               # Transmit rate in Mbps  
        'connected_to': 'if2_to_if1',  # ID of connected interface
        'local_router': 'router1',     # Router this interface belongs to
        'remote_router': 'router2'     # Router on other end
    },
    # ... more interfaces
}

topology = {
    'router1': ['if1_to_if2', 'if1_to_if3'],  # List of interfaces per router
    'router2': ['if2_to_if1', 'if2_to_if3'],
    # ... more routers
}
```

### Output Format
The algorithm returns the same structure, but each telemetry value becomes a tuple:
```python
(original_value, repaired_value, confidence_score)
```

Where:
- `original_value`: The input measurement
- `repaired_value`: Corrected value
- `confidence_score`: Float between 0.0 (uncertain) and 1.0 (confident)

## Initial Algorithm

The baseline algorithm implements:
- Link symmetry validation (my TX ≈ their RX)
- 2% hardening threshold for timing differences
- Confidence scores based on violation magnitude
- Basic status consistency checks

## Evaluation Metrics

The evaluator tests algorithms against multiple perturbation scenarios:

**Perturbation Types:**
1. **Random Zeroing**: 25% of counters dropped to zero
2. **Correlated Zeroing**: 30% of nodes have all counters zeroed
3. **Random Scaling Down**: 20% of counters scaled to 50-80%
4. **Random Scaling Up**: 15% of counters scaled to 120-150%
5. **Correlated Scaling**: 25% of nodes have coordinated scaling

**Metrics:**
- **Counter Repair Accuracy** (75%): How close repaired rates are to ground truth
- **Status Repair Accuracy** (5%): How well interface status is repaired
- **Confidence Calibration** (20%): How well confidence scores reflect actual repair quality
- **Combined Score**: Weighted combination of above metrics

## Usage

### Testing the Initial Algorithm

```bash
cd examples/telemetry_repair
python initial.py
```

### Evaluating a Program

```bash
python evaluate.py --program_path initial.py --results_dir results
```

### Running Evolution

#### Using Hydra Launcher
```bash
cd /Users/audreycc/Documents/Work/LLMTxn/ADRS-Exps/ShinkaEvolve
shinka_launch variant=telemetry_repair_example
```

#### Using Local Runner
```bash
cd examples/telemetry_repair
python run_evo.py
```

## Evolution Strategy

ShinkaEvolve will evolve the algorithm to:
- **Improve detection accuracy** by finding better inconsistency patterns
- **Reduce false positives** through smarter tolerance handling
- **Add sophisticated repairs** using topology relationships and flow conservation
- **Enhance confidence scoring** by considering multiple validation signals
- **Scale to complex networks** with multi-hop relationships

## Example Improvements Evolution Might Discover

- **Dynamic tolerance adjustment** based on network conditions
- **Multi-factor validation** combining several consistency checks
- **Flow conservation repairs** using traffic balance at routers
- **Topology-aware inference** using alternate path information
- **Probabilistic confidence** based on measurement uncertainty
- **Iterative refinement** applying repairs multiple times
- **Anomaly detection** for systematic errors vs random noise

## Dependencies

The example depends on reference files in `openevolve_examples/telemetry_repair`:
- `evaluator.py`: Comprehensive evaluation logic with perturbation scenarios
- `ref_data/`: Ground truth network data and topology files
- `ref_impl/`: Reference implementations (Hodor, DTP) from research

These are automatically accessed via robust path resolution in the evaluation code.

## Configuration

The example uses the following ShinkaEvolve configuration:

- **Task**: `telemetry_repair` (defined in `configs/task/telemetry_repair.yaml`)
- **Variant**: `telemetry_repair_example` (defined in `configs/variant/telemetry_repair_example.yaml`)
- **Evolution**: 300 generations with island-based evolution
- **Models**: Multiple LLMs (GPT-5, Claude, Gemini) with dynamic selection
- **Database**: 3 islands with migration for diversity

## Research Context

This example is based on research on network input validation, specifically:
- **Hodor System**: Three-step validation approach (signal collection, hardening, dynamic checking)
- **Network Invariants**: Link symmetry (R3) and flow conservation (R1)
- **Repair Strategies**: Using redundant signals and topology constraints

The evaluation uses real network data with realistic perturbations to test algorithm robustness.

## Performance Targets

- **Baseline Counter Repair**: ~0.40-0.60 (depends on perturbation type)
- **Target Counter Repair**: 0.70-0.85
- **Confidence Calibration**: Should increase from ~0.30 to 0.60+
- **Combined Score**: Aim for 0.60+ across all scenarios

## Advanced Extensions

Once the basic repair works well, you could extend to:
- **Multi-hop inference** using paths across the network
- **Temporal consistency** using historical measurement trends
- **Probabilistic models** for systematic vs random errors
- **Active probing** to gather additional validation signals
- **Integration** with real network management systems

This example provides a foundation for evolving sophisticated network telemetry validation algorithms that can handle real-world measurement corruption patterns.

