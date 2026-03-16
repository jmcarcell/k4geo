#!/usr/bin/env python3
"""Generate material scan histograms for reference and current branch geometries.

This script:
1. Reads a YAML config listing detector geometries to scan
2. Runs k4run material scans and generates plots for both a reference and current directory
3. Produces consolidated ROOT files for histcmp comparison
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
    parser.add_argument(
        "--skip-reference",
        action="store_true",
        help="Skip reference scanning entirely",
    )
    parser.add_argument(
        "--reference-dir",
        default=None,
        help="Path to an existing k4geo directory to use as reference (e.g. from cvmfs or local)",
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

        canvas = ROOT.TCanvas(f"canvas_{hist_type}", "Material Budget", 800, 600)
        ROOT.SetOwnership(canvas, False)
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
            if obj.IsA() is ROOT.TCanvas.Class():
                canvas = obj
                ROOT.SetOwnership(canvas, False)
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
    """Run k4run material scan. Returns True if the output file was produced."""
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
        subprocess.run(cmd, timeout=300, stdout=stdout, stderr=stderr)
    except subprocess.TimeoutExpired:
        print("ERROR: Material scan timed out (>5min)")
        return False

    # k4run may exit non-zero due to Gaudi finalization errors even when
    # the scan data was produced successfully, so check for the output file.
    if os.path.isfile(output_path):
        return True

    print("ERROR: Material scan failed — output file not produced")
    return False


def run_material_plots(scan_output, output_dir, params):
    """Run material_plots.py to generate histograms. Returns True on success."""
    # Use absolute paths since we cd into output_dir for compatibility with
    # older versions of material_plots.py that don't have --outputDir.
    scan_output = os.path.abspath(scan_output)
    output_dir = os.path.abspath(output_dir)
    script_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "utils", "material_plots.py")
    cmd = [
        "python", script_path,
        "--fname", scan_output,
        "--angleDef", "theta",
        "--angleBinning", str(params["binning"]),
        "--angleMin", str(params["min"]),
        "--angleMax", str(params["max"]),
    ]

    try:
        subprocess.run(cmd, timeout=120, check=True, cwd=output_dir)
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

        compact_dir = os.path.join(source_dir, geometry_name, "compact", compact_name)
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
    return failed_scans


def main():
    ROOT.gROOT.SetBatch(True)

    args = parse_args()
    args.config = os.path.abspath(args.config)

    print("=== Starting material histogram generation ===")

    if not args.skip_reference:
        reference_dir = args.reference_dir or os.environ.get("K4GEO", "")
        if not reference_dir:
            print("ERROR: --reference-dir or K4GEO environment variable is required unless --skip-reference is set")
            sys.exit(1)

        ref_dir = os.path.abspath(reference_dir)
        if not os.path.isdir(ref_dir):
            print(f"ERROR: Reference directory not found: {ref_dir}")
            sys.exit(1)

        print(f"\n=== Using reference directory: {ref_dir} ===")
        original_dir = os.getcwd()
        os.chdir(ref_dir)
        ref_failures = process_geometries(".", os.path.join(original_dir, "detector_geometries_ref.root"), "_ref", args)
        os.chdir(original_dir)

    # Generate current branch histograms
    cur_failures = process_geometries(".", "detector_geometries_monitored.root", "", args)

    print("\n=== Material histogram generation completed ===")
    print("Consolidated files created:")
    if not args.skip_reference:
        print(f"  - detector_geometries_ref.root (from {ref_dir})")
    print("  - detector_geometries_monitored.root")

    all_failures = cur_failures + (ref_failures if not args.skip_reference else [])
    if all_failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
