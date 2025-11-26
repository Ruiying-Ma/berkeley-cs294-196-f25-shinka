#!/usr/bin/env python3
"""
Local evolution runner for telemetry repair example.

This script configures and runs evolution for the network telemetry repair task.
"""

import sys
import hydra
from pathlib import Path
from hydra import initialize, compose
from omegaconf import OmegaConf
from shinka.core import EvolutionRunner


def main():
    """Run the evolution."""
    # Get command-line overrides (skip script name)
    cli_overrides = sys.argv[1:] if len(sys.argv) > 1 else []
    
    # Initialize Hydra with the config from the main configs directory
    with initialize(version_base=None, config_path="../../configs", job_name="telemetry_repair_evolution"):
        base_overrides = [
            "variant@_global_=telemetry_repair_example",
            "job_config.eval_program_path=examples/telemetry_repair/evaluate.py"  # Override for local execution
        ]
        cfg = compose(config_name="config", overrides=base_overrides + cli_overrides)
        
    print("Telemetry Repair Evolution Configuration:")
    print(OmegaConf.to_yaml(cfg, resolve=True))
    
    # Instantiate configs
    job_config = hydra.utils.instantiate(cfg.job_config)
    db_config = hydra.utils.instantiate(cfg.db_config)
    evo_config = hydra.utils.instantiate(cfg.evo_config)
    
    # Run evolution
    evo_runner = EvolutionRunner(
        evo_config=evo_config,
        job_config=job_config,
        db_config=db_config,
        verbose=True,
    )
    evo_runner.run()


if __name__ == "__main__":
    results_data = main()

