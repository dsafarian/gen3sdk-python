import yaml
from itertools import islice, repeat
import json
from fhirpathpy import evaluate
import click
import os, glob
import time
from datetime import datetime, timezone
import shutil
import pathlib
import hashlib
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from cdislogging import get_logger
from gen3.utils import make_folders_for_filename

logging = get_logger("fhir_transform", log_level="info")
TMP_ROOT = pathlib.Path(".fhir_transform/tmp")


class Gen3FHIRAuthzTagger:
    """
    The tagger used to tag FHIR data with appropriate Gen3 compatible authorizations given the rules in a config.yaml file.

    Example:
        tagger = Gen3FHIRAuthzTagger(config_path=config) #create instance of tagger
        tagger.relevant_authz_rules(os.path.basename(input_file).split(".")[0]) #keep only relevant rules for the resource type
        authz_tags = tagger.determine_authz(record) #generate tags
        out = json.dumps(tagger.tag_resource(record, authz_tags)) #tag resources

    """

    def __init__(self, config_path: str | os.PathLike[str], custom_hook=None):
        """
        Initialize instance of the tagger.

        Args:
            config_path (str): the name/path of the config.yaml file
            custom_hook (?): ???
        """

        with open(config_path, "r") as f:
            self.config = yaml.safe_load(f)
        self.custom_hook = custom_hook

    def relevant_authz_rules(self, resource_type: str):
        """
        Filters only the revlevant from the config.yaml file given the resource type.

        Args:
            resource_type (str): the resource type in the .ndjson file

        """
        resource_rules = []
        for rule in self.config.get("rules", []):
            if rule["resource_type"] == resource_type:
                resource_rules.append(rule)
        self.rules = resource_rules

    def determine_authz(self, resource: dict) -> str:
        """
        Determines the correct authorization given the resource type and condition

        Args:
            resource (dict): the resource dictionary (a line of the .ndjson file)

        Returns:
            The appropriate rule/authorization for the resource

        """

        # use custom hook if provided
        if self.custom_hook:
            hook_result = self.custom_hook(resource)
            if hook_result:
                return hook_result

        # check global catch-all fallback
        if "global_authz" in self.config:
            return self.config["global_authz"]

        # resource_type = resource.get("resourceType")

        # check static rules engine via FHIRPath
        #    for rule in self.config.get("rules", []):
        #        if rule["resource_type"] == resource_type:
        #            match = evaluate(resource, rule["condition"])  #how to process in bulk
        #            if match and match[0] is True:
        #                return rule["authz"]
        for rule in self.rules:
            match = evaluate(resource, rule["condition"])
            if match and match[0] is True:  # FIXME: what if multiple conditions match?
                return rule["authz"]

        return None  #### FIXME this is just for testing purposes #####
        raise ValueError(
            f"No authorization tag mapping found for resource ID: {resource.get('id')}"
        )

    def tag_resource(self, resource: dict, authz: str) -> dict:
        """
        Injects the authz path directly into the standard FHIR meta block


        FIXME: match Gen3 FHIR Proxy service requirements here

        Args:
            resource (dict): the resource dictionary (a line of the .ndjson file)
            authz (str): the authorization string to tag the resource with

        Returns:
            resource (dict): tagged resource with the appropriate authz string
        """

        if "meta" not in resource:
            resource["meta"] = {}
        if "security" not in resource["meta"]:
            resource["meta"]["security"] = []

        gen3_security_tag = {
            "system": "http://gen3.org/authz",
            "code": authz,
            "display": f"Gen3 Policy Path: {authz}",
        }

        if gen3_security_tag not in resource["meta"]["security"]:
            resource["meta"]["security"].append(gen3_security_tag)

        return resource

def json_dumps(obj, default=None) -> bytes:
    """Compact UTF-8 JSON bytes. Stand-in for orjson.dumps."""
    return json.dumps(
        obj,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        default=default,
    ).encode("utf-8")

def get_md5hash(input_file: str | os.PathLike[str]) -> str:
    """
    Returns the md5 hash of the input .ndjson file to create a unique folder per file

    Args:
        input_file (str): Input ndjson file to transform

    Returns:
        digest (str): md5 hash of the input .ndjson file

    """
    with open(input_file, "rb") as f:
        digest = hashlib.file_digest(f, "md5").hexdigest()
    return digest


def _is_new(directory: str | os.PathLike[str], record: dict) -> bool:
    """
    Check if directory is new or needs to be rerun. Returns true if directory doesn't exist, if .config.json doesn't exist, or if the the config.yaml file,
    batch_size, or output_file name have changed.

    Args:
        directory (str): path of the directory linked to the .ndjson file
        record (dict): the configuration of the current run, used to compare to what is already saved in the folder

    Returns:
        status (bool): returns whether the directory is new/has to be reset or not
    """

    if not pathlib.Path(directory).is_dir():
        return True

    try:
        params = json.loads(pathlib.Path(directory, ".config.json").read_bytes())
    except:
        return True

    if (
        record["config_hash"] != params["config_hash"]
        or record["batch_size"] != params["batch_size"]
        or record["output_file"] != params["output_file"]
    ):
        return True

    return False


def _is_done(directory: str | os.PathLike[str], record: dict) -> bool:
    """
    Check if transformation in this directory is completed.

    Args:
        directory (str): path of the directory linked to the .ndjson file
        record (dict): the configuration of the current run, used to compare to what is already saved in the folder

    Returns:
        status (bool): returns whether the transformation has beencompleted
    """
    try:
        params = json.loads(pathlib.Path(directory, ".config.json").read_bytes())
        if (
            record["config_hash"] != params["config_hash"]
            or record["batch_size"] != params["batch_size"]
            or record["output_file"] != params["output_file"]
        ):
            return False
    except:
        return False

    # check if output file is the same and if exists
    if (
        record["output_file"] == params["output_file"]
        and pathlib.Path(record["output_file"]).is_file()
        and pathlib.Path(record["output_file"]).stat().st_size != 0
    ):
        logging.info(f"File already tagged. Locate file here: {record['output_file']}")
        return True

    return False


def _resume_run(directory: str | os.PathLike[str], record: dict) -> bool:
    """
    Check if transformation needs to be resumed. Returns True if there are chunk files remaining in the directory.

    Args:
        directory (str): path of the directory linked to the .ndjson file
        record (dict): the configuration of the current run, used to compare to what is already saved in the folder

    Returns:
        status (bool): returns whether the transformation needs to be resumed
    """
    # check for .chunk files --> tagging incomplete
    if any(pathlib.Path(directory).glob("*.chunk")):
        return True
    return False


def _merge_needed(directory: str | os.PathLike[str], record: dict) -> bool:
    """
    Check if .done files have been merged to the final output file. Returns True if .done files remaining in the directory.

    Args:
        directory (str): path of the directory linked to the .ndjson file
        record (dict): the configuration of the current run, used to compare to what is already saved in the folder

    Returns:
        status (bool): returns whether the transformed .done files have to be merged
    """
    # if any .chunk remaining, run is incomplete
    if any(pathlib.Path(directory).glob("*.chunk")):
        return False
    # check for .done files --> merge incomplete
    if any(pathlib.Path(directory).glob("*.done")):
        return True
    return False


def sweep(dry_run: bool = False, force: bool = False) -> int:
    """
    Remove run dirs whose owning process is gone.

    Args:
        dry_run (bool): If True, lists all directories which would be removed, but not actually remove them
        force (bool): If True, delete the whole temporary directory disregarding the status

    Output:
        count(int): number of directories deleted
    """

    if not TMP_ROOT.is_dir():
        logging.info("Nothing to clean")
        return 0

    # force to delete everything disregarding status
    if force:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
        return

    count = 0
    for run_dir in sorted(TMP_ROOT.glob("*")):
        if not run_dir.is_dir():
            continue

        logging.info(f"{'would remove' if dry_run else 'removed '} {run_dir}")
        if not dry_run:
            shutil.rmtree(run_dir, ignore_errors=True)
        count += 1

    logging.info(f"{count} stale run dir(s)")
    return count


def split_file(
    input_file: str | os.PathLike[str],
    batch_size: int,
    output_dir: str | os.PathLike[str],
):
    """
    Split ndjson file into manageable batch-sized chunks

    Args:
        input_file (str): input .ndjson file to split
        batch_size (int): batch size of each chunk
        output_dir (str): the path to the directory where to write all the intermediate files to

    """
    if batch_size < 1:
        raise ValueError(f"batch_size must be >= 1, got {batch_size}")
    start = time.time()
    with open(input_file, "rb") as fin:
        for i, lines in enumerate(iter(lambda: list(islice(fin, batch_size)), [])):
            # with open(f"chunk_{i:05d}.chunk", "wb") as out:
            with open(
                f"{output_dir}/{pathlib.Path(input_file).stem}_{i:05d}.chunk", "wb"
            ) as out:
                out.writelines(lines)
    logging.info(
        f"Split chunks total time: {(time.strftime('%H:%M:%S', time.gmtime(time.time() - start)))}"
    )


def transform_chunk(
    input_file: str | os.PathLike[str],
    tagger: object,
    output_dir: str | os.PathLike[str],
):
    """
    Tag each resource entry with appropriate Gen3 authorization tag based on rules in a config.yaml file

    Args:
        input_file (str): .chunk file to transform
        tagger (object): The tagger instance to use for tagging the resources
        output_dir (str): The directory path of where to write the intermediate files to
    """
    with open(input_file, "rb") as fin, open(
        os.path.join(output_dir, f"{os.path.basename(input_file)}.done"), "wb"
    ) as fout:
        out = bytearray()
        for r in fin:

            if not r.strip():
                continue

            record = json.loads(r)
            authz_tags = tagger.determine_authz(record)  # generate tags
            out += json_dumps(
                tagger.tag_resource(record, authz_tags)
            )  # tag resources)
            out += b"\n"
        fout.write(out)
        os.remove(input_file)  # delete chunk file once it has been transformed


def merge_chunks(
    input_files: list[str | os.PathLike[str]], output_file: str | os.PathLike[str]
):
    """
    Merge all tagged files back to one ndjson file

    Args:
        input_files (list[str]): Tagged files to merge together
        output_file (str): output_file to write the tagged ndjson

    """
    start = time.time()
    out = pathlib.Path(output_file)

    # creates all directories in path if dont exist
    out.parent.mkdir(parents=True, exist_ok=True)

    with open(out, "wb") as fout:
        for f in sorted(input_files):
            with open(f, "rb") as fin:
                shutil.copyfileobj(fin, fout)
    logging.info(
        f"merge chunks total time: {(time.strftime('%H:%M:%S', time.gmtime(time.time() - start)))}"
    )
    for f in input_files:
        os.remove(f)  # remove .done files once merge completed


def fhir_tagger(
    input_file: str | os.PathLike[str],
    output_file: str | os.PathLike[str],
    config: str | os.PathLike[str],
    batch_size: int,
    workers: int = 8,
    force: bool = False,
):
    """

    Stream-transform Bulk FHIR data to Gen3 compatible data with authorization tagging.
    Parallelized line by line tagging with batched I/O

    Args:
            input_file (str): Input .ndjson file
            output_file (str): Output file name
            config (str): .yaml file with authorization rules
            batch_size (int): number of lines per chunk
            workers (int): number of parallel processes
            force (bool): remove all intermediate files for this run before exiting even if it crashes
    """

    start_time = time.time()
    if batch_size < 1:
        raise ValueError(f"batch_size must be >= 1, got {batch_size}")
    if workers < 1:
        raise ValueError(f"workers must be >= 1, got {workers}, default is 8")
    # check if input and output file are the same
    if os.path.realpath(input_file) == os.path.realpath(output_file):
        raise click.UsageError("input_file and output_file must be different")

    make_folders_for_filename(TMP_ROOT)  # only necessary the first time its run

    # make temp directories
    hash = get_md5hash(input_file)
    output_dir = f"{TMP_ROOT}/{os.path.basename(input_file).split('.')[0]}_{hash}"

    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "input_file": input_file,
        "output_file": output_file,
        "config_hash": get_md5hash(config),
        "batch_size": batch_size,
    }

    try:
        logging.info("Checking status of file")

        # if done, skip
        if _is_done(output_dir, record):
            return

        # if new file or new config/batch_size, reset folder
        if _is_new(output_dir, record):
            logging.info("New folder, creating directory and chunking")
            make_folders_for_filename(f"{output_dir}/.config.json")
            pathlib.Path(f"{output_dir}/.config.json").write_text(
                json.dumps(record, default=str), encoding="utf-8"
            )
            # split into chunks
            logging.info(f"Chunking {input_file} into {batch_size}-sized batches...")
            split_file(input_file, batch_size, output_dir)

        # initialize tagger
        tagger = Gen3FHIRAuthzTagger(config_path=config)  # create instance of tagger
        tagger.relevant_authz_rules(
            os.path.basename(input_file).split(".")[0]
        )  # keep only relevant rules for the resource type

        # if not fully transformed or new/reset, resume
        if not _merge_needed(output_dir, record):
            chunk_files = glob.glob(
                os.path.join(output_dir, f"{pathlib.Path(input_file).stem}_*.chunk")
            )  # paths for chunk files

            # parallelize transform
            logging.info("Transforming chunks...")
            start = time.time()
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for _ in pool.map(
                    transform_chunk, chunk_files, repeat(tagger), repeat(output_dir)
                ):
                    pass
            logging.info(
                f"Transform chunks total time: {(time.strftime('%H:%M:%S', time.gmtime(time.time() - start)))}"
            )

        # merge back to one ndjson file
        transformed_files = glob.glob(
            os.path.join(output_dir, f"{pathlib.Path(input_file).stem}_*.done")
        )  # paths for transformed files
        logging.info(f"Merging chunks to {output_file}...")
        merge_chunks(transformed_files, output_file)

        elapsed_time = time.time() - start_time
        logging.info(f"Tagged file -> {output_file}")
        # logging.info(f"Total time to process: {(time.strftime('%H:%M:%S', time.gmtime(elapsed_time)))}")
        logging.info(
            f"Total time to process: {(time.strftime('%H:%M:%S', time.gmtime(elapsed_time)))}"
        )
        if force:
            shutil.rmtree(output_dir, ignore_errors=True)

    except BaseException as e:
        logging.error(e)
        if force:
            shutil.rmtree(output_dir, ignore_errors=True)
        else:
            logging.error("run failed; intermediates left in %s", output_dir)
