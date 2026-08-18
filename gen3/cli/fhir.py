from fhirpathpy import evaluate
import click
import os
import pathlib
from cdislogging import get_logger
from gen3.fhir import *

logging = get_logger("fhir_transform", log_level="info")
TMP_ROOT = pathlib.Path(".fhir_transform/tmp")


@click.group()
def fhir():
    """Commands for FHIR data processing"""
    pass


@click.command(
    context_settings={"help_option_names": ["-h", "--help"]},
    help="Tag Bulk FHIR data with Gen3 compatible authorization tags",
)
@click.argument(
    "input_file",
    type=click.Path(exists=True, dir_okay=False, readable=True),
    metavar="input_file",
)
@click.argument(
    "output_file", type=click.Path(dir_okay=False, writable=True), metavar="output_file"
)
@click.argument(
    "config",
    type=click.Path(exists=True, dir_okay=False, readable=True),
    metavar="config",
)
@click.option(
    "-b",
    "--batch_size",
    type=click.IntRange(min=1),
    default=10000,
    show_default=True,
    metavar="batch_size",
    help="batch size for chunking",
)
@click.option(
    "-w",
    "--workers",
    type=click.IntRange(min=1),
    default=8,
    show_default=True,
    metavar="workers",
    help="number of parallel processes",
)
@click.option(
    "--force",
    is_flag=True,
    help="Remove all intermediate files for this run before exiting even if run crashes",
)
def cli(
    input_file: str | os.PathLike[str],
    output_file: str | os.PathLike[str],
    config: str | os.PathLike[str],
    workers: int,
    batch_size: int,
    force: bool,
):
    """
    CLI implementation of fhir_tagger.

    Args:
        input_file (str): Input .ndjson file
        output_file (str): Output file name
        config (str): .yaml file with authorization rules
        batch_size (int): numb er of lines per chunk
        workers (int): number of parallel processes
        force (bool): remove all intermediate files for this run before exiting even if it crashes
    """
    fhir_tagger(input_file, output_file, config, workers, batch_size, force)


@click.command(
    context_settings={"help_option_names": ["-h", "--help"]},
    help="Remove all intermediate files in the tmp folder from previous runs",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Used with --cleanup, report what would be deleted with --cleanup without deleting the files",
)
@click.option(
    "--force",
    is_flag=True,
    help="Remove temporary directory ignoring status of each directory",
)
def cleanup(dry_run: bool, force: bool):
    """
    Remove all intermediate files in the tmp folder from previous runs

    Args:
        dry_run (bool): If True, list the files that would be removed, but not actually remove them
        force (bool): Delete all intermediate directories disregarding the status
    """

    sweep(dry_run=dry_run, force=force)


fhir.add_command(cli, name="transform")
fhir.add_command(cleanup, name="cleanup")
