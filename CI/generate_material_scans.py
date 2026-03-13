#!/usr/bin/env python3
"""Generate material scan histograms for reference and current branch geometries.

This script:
1. Reads a YAML config listing detector geometries to scan
2. Detects the comparison target (branch/repo) from environment or git
3. Clones the reference branch
4. Runs k4run material scans and generates plots for both branches
5. Produces consolidated ROOT files for histcmp comparison
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile

import yaml
import ROOT


def parse_args():
    parser = argparse.ArgumentParser(description="Material scan histogram generator")
    parser.add_argument(
        "-q", "--quiet", action="store_true", help="Suppress verbose output from k4run"
    )
    parser.add_argument(
        "-f",
        "--fast",
        action="store_true",
        help="Reduced angular resolution for quicker scanning",
    )
    parser.add_argument(
        "-c",
        "--config",
        default="CI/config/geometry_list.yml",
        help="Geometry configuration file (default: CI/config/geometry_list.yml)",
    )
    return parser.parse_args()


def read_geometry_list(config_path):
    """Read geometry list from YAML config file."""
    if not os.path.isfile(config_path):
        print(f"ERROR: Geometry configuration file not found: {config_path}")
        sys.exit(1)

    with open(config_path) as f:
        config = yaml.safe_load(f)

    geometries = config.get("geometries", [])
    if not geometries:
        print(f"ERROR: No geometries found in config file: {config_path}")
        sys.exit(1)

    print(f"Reading geometry list from: {config_path}")
    print(f"Found {len(geometries)} geometries in config:")
    for geom in geometries:
        print(f"  - {geom}")

    return geometries


def detect_comparison_target():
    """Detect the branch and repo to compare against."""
    print("=== Detecting comparison target ===")

    base_ref = os.environ.get("GITHUB_BASE_REF", "")
    repository = os.environ.get("GITHUB_REPOSITORY", "")

    # Method 1: GitHub Actions environment
    if base_ref and repository:
        target_branch = base_ref
        target_repo = f"https://github.com/{repository}.git"
        print(f"Detected from GitHub Actions: {target_branch} @ {target_repo}")
        return target_branch, target_repo

    # Method 2: Git commands
    try:
        subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            check=True,
            capture_output=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("Not in a git repository, using defaults")
        return "main", "https://github.com/key4hep/k4geo.git"

    try:
        target_repo = (
            subprocess.run(
                ["git", "remote", "get-url", "origin"],
                capture_output=True,
                text=True,
                check=True,
            )
            .stdout.strip()
        )
    except subprocess.CalledProcessError:
        target_repo = "https://github.com/key4hep/k4geo.git"

    try:
        result = subprocess.run(
            ["git", "ls-remote", "--symref", "origin", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        # Parse "ref: refs/heads/main\tHEAD"
        first_line = result.stdout.splitlines()[0]
        target_branch = first_line.split("refs/heads/")[-1].split("\t")[0]
    except (subprocess.CalledProcessError, IndexError):
        target_branch = "main"

    print(f"Detected from git: {target_branch} @ {target_repo}")
    return target_branch, target_repo


def create_empty_histograms(output_dir, compact_name, error_message):
    """Create empty placeholder ROOT histograms for a failed scan."""
    print(f"Creating empty placeholder histograms for failed scan: {compact_name}")

    for hist_type, title in [
        ("x0", "X0 Material Budget [%]"),
        ("lambda", "Lambda Interaction Length"),
        ("depth", "Material Depth"),
    ]:
        filepath = os.path.join(output_dir, f"{hist_type}.root")
        outfile = ROOT.TFile.Open(filepath, "RECREATE")
        if not outfile or outfile.IsZombie():
            print(f"ERROR: Cannot create empty histogram file {filepath}")
            continue

        empty_hist = ROOT.TH1F("empty_hist", f"FAILED SCAN: {error_message}", 100, 0, 180)
        empty_hist.SetTitle(f"{title} - SCAN FAILED: {compact_name}")

        canvas = ROOT.TCanvas("canvas", "Material Budget", 800, 600)
        stack = ROOT.THStack("stack", title)
        empty_hist.SetFillColor(ROOT.kRed)
        empty_hist.SetLineColor(ROOT.kRed)
        stack.Add(empty_hist)

        canvas.cd()
        stack.Draw("hist")

        error_text = ROOT.TText(0.5, 0.5, f"SCAN FAILED: {error_message}")
        error_text.SetNDC()
        error_text.SetTextAlign(22)
        error_text.SetTextColor(ROOT.kRed)
        error_text.SetTextSize(0.05)
        error_text.Draw()

        canvas.Write()
        outfile.Close()
        print(f"Created empty histogram: {filepath}")


def add_to_consolidated(input_dir, output_file, hist_prefix):
    """Add histograms from input_dir to the consolidated ROOT file."""
    outfile = ROOT.TFile.Open(output_file, "UPDATE")
    if not outfile or outfile.IsZombie():
        print(f"ERROR: Cannot open consolidated file {output_file}")
        return

    for hist_type in ("x0", "lambda", "depth"):
        input_path = os.path.join(input_dir, f"{hist_type}.root")
        infile = ROOT.TFile.Open(input_path, "READ")
        if not infile or infile.IsZombie():
            print(f"WARNING: Cannot open {input_path}")
            continue

        # Find canvas in file
        canvas = None
        for key in infile.GetListOfKeys():
            obj = key.ReadObj()
            if obj.IsA() == ROOT.TCanvas.Class():
                canvas = obj
                break

        if not canvas:
            print(f"WARNING: No canvas found in {input_path}")
            infile.Close()
            continue

        if canvas.GetListOfPrimitives().GetEntries() < 2:
            print(f"WARNING: Canvas has insufficient primitives in {input_path}")
            infile.Close()
            continue

        obj1 = canvas.GetListOfPrimitives().At(1)
        if not obj1 or not obj1.InheritsFrom("THStack"):
            print(f"WARNING: No THStack found in {input_path}")
            infile.Close()
            continue

        stack = obj1
        hists = stack.GetHists()
        if not hists or hists.GetEntries() == 0:
            print(f"WARNING: No histograms in stack from {input_path}")
            infile.Close()
            continue

        # Create combined histogram by summing all in the stack
        combined = hists.At(0).Clone()
        for j in range(1, hists.GetEntries()):
            combined.Add(hists.At(j))

        hist_name = f"{hist_prefix}-{hist_type}"
        combined.SetName(hist_name)
        combined.SetTitle(f"Material Budget: {hist_prefix} {hist_type}")

        outfile.cd()
        combined.Write()
        print(f"Added histogram: {hist_name}")
        infile.Close()

    outfile.Close()


def run_material_scan(xml_file, output_path, params, quiet):
    """Run k4run material scan. Returns True on success."""
    cmd = [
        "k4run",
        "utils/material_scan.py",
        "--GeoSvc.detector", xml_file,
        "--GeoDump.filename", output_path,
        "--angleDef", "theta",
        "--angleBinning", str(params["binning"]),
        "--angleMin", str(params["min"]),
        "--angleMax", str(params["max"]),
        "--nPhi", str(params["nphi"]),
    ]

    stdout = subprocess.DEVNULL if quiet else None
    stderr = subprocess.DEVNULL if quiet else None

    try:
        subprocess.run(cmd, timeout=300, check=True, stdout=stdout, stderr=stderr)
        return True
    except subprocess.TimeoutExpired:
        print(f"ERROR: Material scan timed out (>5min)")
        return False
    except subprocess.CalledProcessError as e:
        code = e.returncode
        reasons = {-11: "Segmentation fault", -6: "SIGABRT signal", -9: "SIGKILL signal"}
        reason = reasons.get(code, f"Exit code {code}")
        print(f"ERROR: Material scan failed: {reason}")
        return False


def run_material_plots(scan_output, output_dir, params):
    """Run material_plots.py to generate histograms. Returns True on success."""
    cmd = [
        "python", "utils/material_plots.py",
        "-f", scan_output,
        "-o", output_dir,
        "--angleDef", "theta",
        "--angleBinning", str(params["binning"]),
        "--angleMin", str(params["min"]),
        "--angleMax", str(params["max"]),
    ]

    try:
        subprocess.run(cmd, timeout=120, check=True)
        return True
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
        return False


def process_geometries(source_dir, consolidated_file, file_suffix, args):
    """Run material scans for all configured geometries and build consolidated ROOT file."""
    print(f"\n=== Processing geometries from {source_dir} into {consolidated_file} ===")

    geometry_list = read_geometry_list(args.config)

    if args.fast:
        params = {"binning": 1, "min": 0, "max": 180, "nphi": 10}
        print("FAST MODE: Using reduced angular resolution")
    else:
        params = {"binning": 1, "min": 0, "max": 180, "nphi": 100}

    # Initialize consolidated ROOT file
    outfile = ROOT.TFile.Open(consolidated_file, "RECREATE")
    if not outfile or outfile.IsZombie():
        print(f"ERROR: Cannot create consolidated file {consolidated_file}")
        sys.exit(1)
    outfile.Close()

    failed_scans = []
    total_processed = 0
    successful_scans = 0

    for geometry_path in geometry_list:
        geometry_name = os.path.dirname(geometry_path)
        compact_name = os.path.basename(geometry_path)

        compact_dir = os.path.join(source_dir, "FCCee", geometry_name, "compact", compact_name)
        xml_file = os.path.join(compact_dir, f"{compact_name}.xml")

        print(f"\nProcessing configured geometry: {geometry_path}")

        if not os.path.isfile(xml_file):
            print(f"Warning: XML file not found: {xml_file}")
            failed_scans.append(f"{compact_name} (XML file not found)")
            continue

        total_processed += 1

        # Create temp output directory
        output_dir = tempfile.mkdtemp(prefix=f"matscan_{compact_name}_")
        scan_output = os.path.join(output_dir, f"out_material_scan{file_suffix}.root")

        try:
            # Run material scan
            if args.quiet:
                print(f"Processing: {xml_file} (output suppressed)")
            else:
                print(f"Processing: {xml_file}")

            print(f"Starting material scan for {compact_name}...")
            scan_ok = run_material_scan(xml_file, scan_output, params, args.quiet)

            if scan_ok and os.path.isfile(scan_output):
                print(f"Material scan completed successfully for {compact_name}")

                # Generate plots
                print("Generating material plots...")
                plot_ok = run_material_plots(scan_output, output_dir, params)

                if plot_ok and os.path.isfile(os.path.join(output_dir, "x0.root")):
                    print(f"Plot generation completed successfully for {compact_name}")
                    successful_scans += 1
                else:
                    print(f"Plot generation FAILED for {compact_name}")
                    failed_scans.append(f"{compact_name} (Plot generation failed)")
                    create_empty_histograms(output_dir, compact_name, "Plot generation failed")
            else:
                print(f"MATERIAL SCAN FAILED for {compact_name}")
                failed_scans.append(f"{compact_name} (scan failed)")
                create_empty_histograms(output_dir, compact_name, "Scan failed")

            # Add histograms to consolidated file
            print(f"Adding histograms to consolidated file for {compact_name}")
            add_to_consolidated(output_dir, consolidated_file, compact_name)

        finally:
            shutil.rmtree(output_dir, ignore_errors=True)

    # Verify consolidated file
    verify = ROOT.TFile.Open(consolidated_file, "READ")
    if verify and not verify.IsZombie():
        print(f"\n=== Final consolidated file contents ===")
        verify.ls()
        verify.Close()

    # Report
    print(f"\n=== MATERIAL SCAN SUMMARY ===")
    print(f"Total geometries processed: {total_processed}")
    print(f"Successful scans: {successful_scans}")
    print(f"Failed scans: {len(failed_scans)}")

    if failed_scans:
        print(f"\nFAILED SCANS DETECTED:")
        for failed in failed_scans:
            print(f"  - {failed}")

        with open("material_scan_errors.md", "w") as f:
            f.write("# Material Scan Error Summary\n")
            f.write(f"\n## Summary\n")
            f.write(f"- Total processed: {total_processed}\n")
            f.write(f"- Successful: {successful_scans}\n")
            f.write(f"- Failed: {len(failed_scans)}\n")
            f.write(f"\n## Failed Scans\n")
            for failed in failed_scans:
                f.write(f"- {failed}\n")

        print("Error summary saved to: material_scan_errors.md")
    else:
        print("All scans completed successfully!")

    print("==========================")


def clone_reference(target_repo, target_branch, quiet):
    """Clone the reference branch. Returns the clone directory path."""
    clone_dir = "k4geo_main_ref"
    print(f"\n=== Cloning comparison target ===")
    print(f"Repository: {target_repo}")
    print(f"Branch: {target_branch}")

    stdout = subprocess.DEVNULL if quiet else None
    stderr = subprocess.DEVNULL if quiet else None

    try:
        subprocess.run(
            ["git", "clone", "--branch", target_branch, "--depth", "1", target_repo, clone_dir],
            check=True,
            stdout=stdout,
            stderr=stderr,
        )
    except subprocess.CalledProcessError:
        print(f"Failed to clone {target_branch} from {target_repo}, trying upstream main...")
        try:
            subprocess.run(
                [
                    "git", "clone", "--branch", "main", "--depth", "1",
                    "https://github.com/key4hep/k4geo.git", clone_dir,
                ],
                check=True,
                stdout=stdout,
                stderr=stderr,
            )
        except subprocess.CalledProcessError:
            print("FATAL: Could not clone any reference branch")
            sys.exit(1)

    return clone_dir


def main():
    ROOT.gROOT.SetBatch(True)

    args = parse_args()
    args.config = os.path.abspath(args.config)
    target_branch, target_repo = detect_comparison_target()

    print("=== Starting material histogram generation ===")

    # Clone reference and generate reference histograms
    clone_dir = clone_reference(target_repo, target_branch, args.quiet)

    original_dir = os.getcwd()
    os.chdir(clone_dir)
    process_geometries(".", os.path.join(original_dir, "detector_geometries_ref.root"), "_ref", args)
    os.chdir(original_dir)

    # Generate current branch histograms
    process_geometries(".", "detector_geometries_monitored.root", "", args)

    # Clean up
    shutil.rmtree(clone_dir, ignore_errors=True)

    print("\n=== Material histogram generation completed ===")
    print("Consolidated files created:")
    print("  - detector_geometries_ref.root")
    print("  - detector_geometries_monitored.root")


if __name__ == "__main__":
    main()
