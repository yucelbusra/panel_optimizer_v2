# -*- coding: utf-8 -*-

import os
import sys

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if THIS_DIR not in sys.path:
    sys.path.insert(0, THIS_DIR)

from export import run_export
from config_ui import run_optimizer_from_folder
from place import run_placement

def main():
    print("=== Running Full Panel Pipeline ===")

    # Step 1: export walls/openings
    input_dir = run_export()
    if not input_dir:
        print("Export canceled or failed.")
        return

    # Step 2: show config UI + run optimizer
    optimizer_result = run_optimizer_from_folder(input_dir)
    if not optimizer_result:
        print("Optimizer canceled or failed.")
        return

    # Step 3: place panels
    run_placement(optimizer_result)

    print("=== Pipeline completed successfully ===")

if __name__ == "__main__":
    main()
