"""Test `fastq_ingress` result of previously run workflow."""
import argparse
import json
import os
import pathlib
import sys

import pandas as pd
import pysam
import pytest


FASTQ_EXTENSIONS = ["fastq", "fastq.gz", "fq", "fq.gz"]
ROOT_DIR = pathlib.Path(__file__).resolve().parent.parent


def is_fastq_file(fname):
    """Check if file is a FASTQ file."""
    return any(map(lambda ext: fname.endswith(ext), FASTQ_EXTENSIONS))


def create_metadict(**kwargs):
    """Create dict from metadata and check if required values are present."""
    if "alias" not in kwargs or kwargs["alias"] is None:
        raise ValueError("Meta data needs 'alias'.")
    defaults = dict(barcode=None, type="test_sample")
    defaults.update(kwargs)
    return defaults


def get_fastq_entries(fastq_file):
    """Create a list of names of entries in a FASTQ file."""
    entries = []
    with pysam.FastxFile(fastq_file) as f:
        for entry in f:
            entries.append(entry.name)
    return entries


def args():
    """Parse and process input arguments. Use the workflow params for those missing."""
    # get the path to the workflow output directory
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--wf-output-dir",
        default=ROOT_DIR / "output",
        help=(
            "path to the output directory where the workflow results have been "
            "published; defaults to 'output' in the root directory of the workflow if "
            "not provided"
        ),
    )
    parser.add_argument(
        "--fastq",
        help=(
            "Path to FASTQ input file / directory with FASTQ files / sub-directories; "
            "will take input path from workflow output if not provided"
        ),
    )
    parser.add_argument(
        "--sample_sheet",
        help=(
            "Path to sample sheet CSV file. If not provided, will take sample sheet "
            "path from workflow params (if available)."
        ),
    )
    args = parser.parse_args()

    wf_output_dir = pathlib.Path(args.wf_output_dir)
    fastq_ingress_results_dir = wf_output_dir / "fastq_ingress_results"

    # make sure that there are fastq_ingress results (i.e. that the workflow has been
    # run successfully and that the correct wf output path was provided)
    if not fastq_ingress_results_dir.exists():
        raise ValueError(
            f"{fastq_ingress_results_dir} does not exist. Has `wf-template` been run?"
        )

    # get the workflow params
    with open(wf_output_dir / "params.json", "r") as f:
        params = json.load(f)
    input_path = args.fastq if args.fastq is not None else ROOT_DIR / params["fastq"]
    sample_sheet = args.sample_sheet
    if sample_sheet is None and params["sample_sheet"] is not None:
        sample_sheet = ROOT_DIR / params["sample_sheet"]

    if not os.path.exists(input_path):
        raise ValueError(f"Input path '{input_path}' does not exist.")

    return input_path, sample_sheet, fastq_ingress_results_dir, params


def get_valid_inputs(input_path, sample_sheet, params):
    """Get valid input paths and corresponding metadata."""
    # find the valid inputs
    valid_inputs = []
    if os.path.isfile(input_path):
        # handle file case
        valid_inputs.append(
            [
                create_metadict(
                    alias=params["sample"]
                    if params["sample"] is not None
                    else os.path.basename(input_path).split(".")[0]
                ),
                input_path,
            ]
        )
    else:
        # is a directory --> check if fastq files in top-level dir or in sub-dirs
        tree = list(os.walk(input_path))
        top_dir_has_fastq_files = any(map(is_fastq_file, tree[0][2]))
        subdirs_have_fastq_files = any(
            any(map(is_fastq_file, files)) for _, _, files in tree[1:]
        )
        if top_dir_has_fastq_files and subdirs_have_fastq_files:
            raise ValueError(
                f"Input directory '{input_path}' cannot contain FASTQ "
                "files and sub-directories with FASTQ files."
            )
        # make sure we only have fastq files in either (top-level dir or sub-dirs) and
        # not both
        if not top_dir_has_fastq_files and not subdirs_have_fastq_files:
            raise ValueError(
                f"Input directory '{input_path}' contains neither sub-directories "
                "nor FASTQ files."
            )
        if top_dir_has_fastq_files:
            valid_inputs.append(
                [
                    create_metadict(
                        alias=params["sample"]
                        if params["sample"] is not None
                        else os.path.basename(input_path)
                    ),
                    input_path,
                ]
            )
        else:
            # iterate over the sub-directories
            for subdir, subsubdirs, files in tree[1:]:
                # make sure we don't have sub-sub-directories containing fastq files
                if subsubdirs and any(
                    is_fastq_file(file)
                    for subsubdir in subsubdirs
                    for file in os.listdir(pathlib.Path(subdir) / subsubdir)
                ):
                    raise ValueError(
                        f"Input directory '{input_path}' cannot contain more "
                        "than one level of sub-directories with FASTQ files."
                    )
                # handle unclassified
                if (
                    os.path.basename(subdir) == "unclassified"
                    and not params["analyse_unclassified"]
                ):
                    continue
                # only process further if sub-dir has fastq files
                if any(map(is_fastq_file, files)):
                    barcode = os.path.basename(subdir)
                    valid_inputs.append(
                        [create_metadict(alias=barcode, barcode=barcode), subdir]
                    )
    # parse the sample sheet in case there was one
    if sample_sheet is not None:
        sample_sheet = pd.read_csv(sample_sheet).set_index(
            # set 'barcode' as index while also keeping the 'barcode' column in the df
            "barcode",
            drop=False,
        )
        # now, get the corresponding inputs for each entry in the sample sheet (sample
        # sheet entries for which no input directory was found will have `None` as their
        # input path); we need a dict mapping barcodes to valid input paths for this
        valid_inputs_dict = {os.path.basename(path): path for _, path in valid_inputs}
        # reset `valid_inputs`
        valid_inputs = []
        for barcode, meta in sample_sheet.iterrows():
            path = valid_inputs_dict.get(barcode)  # returns `None` if key not found
            valid_inputs.append([create_metadict(**dict(meta)), path])
    return valid_inputs


# prepare data for the tests
@pytest.fixture(scope="module")
def prepare():
    """Prepare data for tests."""
    input_path, sample_sheet, fastq_ingress_results_dir, params = args()
    valid_inputs = get_valid_inputs(input_path, sample_sheet, params)
    return fastq_ingress_results_dir, valid_inputs, params


# define tests
def test_result_subdirs(prepare):
    """
    Test if workflow results dir contains all expected samples.

    Tests if the published sub-directories in `fastq_ingress_results_dir` contain all
    the samples we expect.
    """
    fastq_ingress_results_dir, valid_inputs, _ = prepare
    _, subdirs, files = next(os.walk(fastq_ingress_results_dir))
    assert not files, "Files found in top-level dir of fastq_ingress results"
    assert set(subdirs) == set([meta["alias"] for meta, _ in valid_inputs])


def test_fastq_entry_names(prepare):
    """
    Test FASTQ entries.

    Tests if the concatenated sequences indeed contain all the FASTQ entries of the
    FASTQ files in the valid inputs.
    """
    fastq_ingress_results_dir, valid_inputs, _ = prepare
    for meta, path in valid_inputs:
        if path is None:
            # this sample sheet entry had no input dir (or no reads)
            continue
        # get FASTQ entries in the result file produced by the workflow
        fastq_entries = get_fastq_entries(
            fastq_ingress_results_dir / meta["alias"] / "seqs.fastq.gz"
        )
        # now collect the FASTQ entries from the individual input files
        exp_fastq_entries = []
        fastq_files = (
            filter(is_fastq_file, os.listdir(path)) if os.path.isdir(path) else [path]
        )
        for fastq_file in fastq_files:
            exp_fastq_entries += get_fastq_entries(pathlib.Path(path) / fastq_file)
        assert set(fastq_entries) == set(exp_fastq_entries)


def test_stats_present(prepare):
    """Tests if the `fastcat` stats are present when they should be."""
    fastq_ingress_results_dir, valid_inputs, params = prepare
    for meta, path in valid_inputs:
        if path is None:
            # this sample sheet entry had no input dir (or no reads)
            continue
        # we expect `fastcat` stats in two cases: (i) they were requested explicitly or
        # (ii) the input was a directory containing multiple FASTQ files
        expect_stats = (
            params["wf"]["fastcat_stats"]
            or os.path.isdir(path)
            and len(list(filter(is_fastq_file, os.listdir(path)))) > 1
        )
        stats_dir = fastq_ingress_results_dir / meta["alias"] / "fastcat_stats"
        # assert that stats are there when we expect them
        assert expect_stats == stats_dir.exists()
        # make sure that the per-file and per-read stats files are there
        if expect_stats:
            for fname in ("per-file-stats.tsv", "per-read-stats.tsv"):
                assert (
                    fastq_ingress_results_dir / meta["alias"] / "fastcat_stats" / fname
                ).is_file()


def test_metamap(prepare):
    """Test if the metamap in the `fastq_ingress` results is as expected."""
    fastq_ingress_results_dir, valid_inputs, _ = prepare
    for meta, _ in valid_inputs:
        with open(fastq_ingress_results_dir / meta["alias"] / "metamap.json", "r") as f:
            metamap = json.load(f)
        assert meta == metamap


if __name__ == "__main__":
    # trigger pytest
    ret_code = pytest.main([os.path.realpath(__file__)])
    sys.exit(ret_code)
