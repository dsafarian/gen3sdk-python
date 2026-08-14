import pytest
import math
from transform_utils import Gen3FHIRAuthzTagger, _is_new, _is_done, _resume_run, _merge_needed, sweep, split_file, transform_chunk, merge_chunks, fhir_tagger
import pathlib
import os
import orjson
import subprocess
import sys
import shutil


TMP_ROOT = pathlib.Path("./outputs")
TAGGER = Gen3FHIRAuthzTagger("config.yaml")
SRC = TMP_ROOT / "test_Patient.ndjson"
IN = TMP_ROOT / "Patient.ndjson"
OUT = os.path.join(TMP_ROOT, "output.ndjson")
BATCH_SIZE = 1
BASE_RECORD = {
    "timestamp":"2026-08-12T21:00:20.677168+00:00",
    "input_file":"output/fhir/Patient.ndjson",
    "output_file":"outputs/test_status/status.ndjson",
    "config_hash":"9c61e70e3ea403e3daf1ee795036253c",
    "batch_size":1}

os.makedirs(TMP_ROOT, exist_ok=True)


def mock_state(
    directory,
    *,
    config="match",        # "match" | None (no file) | dict (custom) | str (raw bytes)
    chunks=0,
    done=0,
    output=None,           # None | "empty" | "full"
    record=None,
):
    """
    Returns (directory, record) with the directory populated as described.
    The record's output_file always points inside TMP_ROOT
    """
    record = dict(record or BASE_RECORD)
    out = pathlib.Path(directory) / "status.ndjson"
    record["output_file"] = str(out)
    
 
    shutil.rmtree(directory, ignore_errors=True)
    directory.mkdir(parents=True)
 
    if config == "match":
        params = dict(record)
    elif isinstance(config, dict):
        params = dict(record)
        params.update(config)
    else:
        params = None
 
    if params is not None:
        (directory / ".config.json").write_bytes(orjson.dumps(params))
    elif isinstance(config, str) and config != "match":
        (directory / ".config.json").write_text(config, encoding="utf-8")
 
    for i in range(chunks):
        (directory / f"chunk_{i:03d}.chunk").write_text("{}\n", encoding="utf-8")
    for i in range(done):
        (directory / f"chunk_{i:03d}.done").write_text("{}\n", encoding="utf-8")

   
    if output == "empty":
        out.touch()
    elif output == "full":
        out.write_text('{"id":1}\n', encoding="utf-8")
    

    return directory, record

def test_fhir_output():
    src = [orjson.loads(line)
        for line in pathlib.Path(SRC).read_bytes().splitlines()
        if line.strip()]
    fhir_tagger(IN, OUT, "config.yaml", 8, BATCH_SIZE)
    out = [orjson.loads(line)
        for line in pathlib.Path(OUT).read_bytes().splitlines()
        if line.strip()]
    
    assert len(src) == len(out)
    assert {r["id"] for r in src} == {r["id"] for r in out}
    assert [ {k: v for k, v in r.items() if k != "meta.security"} for r in out ] == src


def test_chunking():
    split_file(IN, BATCH_SIZE, TMP_ROOT)
    chunks = list(TMP_ROOT.glob("*.chunk"))
    fin = [orjson.loads(line)
        for line in pathlib.Path(IN).read_bytes().splitlines()
        if line.strip()]
    expected = math.ceil(len(fin) / BATCH_SIZE)
    assert len(chunks) == expected, f"expected {expected} chunks, found {len(chunks)}"
    recombined = [orjson.loads(line) for p in sorted(chunks) for line in p.read_bytes().splitlines() if line.strip()]
    assert recombined == fin

def test_transform():
    TAGGER.relevant_authz_rules(os.path.basename(IN).split(".")[0])
    chunks = list(TMP_ROOT.glob("*.chunk"))
    for c in chunks:
        transform_chunk(c, TAGGER, TMP_ROOT)
    transformed = list(TMP_ROOT.glob("*.done"))
    assert len(transformed) == len(chunks)
    assert len(list(TMP_ROOT.glob("*.chunk"))) == 0
    

def test_merge():
    transformed = list(TMP_ROOT.glob("*.done"))
    transformed_sum = sum(len([l for l in pathlib.Path(c).read_bytes().splitlines() if l.strip()]) for c in transformed)
    merge_chunks(transformed, OUT)
    merged = pathlib.Path(OUT).read_text(encoding="utf-8")
    fin = [orjson.loads(line)
            for line in pathlib.Path(IN).read_bytes().splitlines()
            if line.strip()]
    fout = fin = [orjson.loads(line)
            for line in pathlib.Path(OUT).read_bytes().splitlines()
            if line.strip()]
    assert os.path.exists(OUT)
    assert pathlib.Path(OUT).stat().st_size > 0
    assert len(list(TMP_ROOT.glob("*.done"))) == 0
    assert len(fout) == transformed_sum
    assert len(fout) == len(fin)
    assert merged.endswith("\n"), "file does not end with a newline"
    assert not merged.endswith("\n\n"), "file ends with a blank line"
    for i, line in enumerate(merged.split("\n")[:-1]):
        assert line.strip(), f"blank line at index {i}"
        orjson.loads(line)  # every line independently parses


def test_is_new():
    tmp_path = TMP_ROOT / "test_new"
    #directory missing
    assert _is_new(TMP_ROOT / "does_not_exist", BASE_RECORD) is True

    #no config file
    directory, record = mock_state(tmp_path, config=None)
    assert _is_new(directory, record) is True

    #check corrupt config file
    directory, record = mock_state(tmp_path, config="{not valid json")
    assert _is_new(directory, record) is True

    #check empty config file
    directory, record = mock_state(tmp_path, config="")
    assert _is_new(directory, record) is True

    #check if not new when everything matches
    directory, record = mock_state(tmp_path, config="match")
    assert _is_new(directory, record) is False

    #check when config changed
    directory, record = mock_state(tmp_path, config={"config_hash": "different"})
    assert _is_new(directory, record) is True

    #check when batch_size changed
    directory, record = mock_state(tmp_path, config={"batch_size": 50})
    assert _is_new(directory, record) is True

    #check when output filename changed
    directory, record = mock_state(tmp_path, config={"output_file": "/tmp/somewhere_else.ndjson"})
    assert _is_new(directory, record) is True



def test_is_done():
    tmp_path = TMP_ROOT / "test_done"

    #config matches but output is empty
    directory, record = mock_state(tmp_path, config="match", output="empty")
    assert _is_done(directory, record) is False

    #config matches but no output
    directory, record = mock_state(tmp_path, config="match", output=None)
    assert _is_done(directory, record) is False
    
    #output full but no config file
    directory, record = mock_state(tmp_path, config=None, output="full")
    assert _is_done(directory, record) is False

    #output full but config file is corrupt
    directory, record = mock_state(tmp_path, config="{bad", output="full")
    assert _is_done(directory, record) is False

    #output full batch_size changed
    directory, record = mock_state(tmp_path, config={"batch_size": 50}, output="full")
    assert _is_done(directory, record) is False

    #output full but config changed
    directory, record = mock_state(tmp_path, config={"config_hash": "different"}, output="full")
    assert _is_done(directory, record) is False

    #output full but output filename changed
    directory, record = mock_state(tmp_path, config={"output_file": "/tmp/somewhere_else.ndjson"}, output="full")
    assert _is_done(directory, record) is False

     #config matches and output is full
    directory, record = mock_state(tmp_path, config="match", output="full")
    assert _is_done(directory, record) is True


def test_resume_run():
    tmp_path = TMP_ROOT / "test_resume"
    #no chunk files in directory
    directory, record = mock_state(tmp_path, chunks=0)
    assert _resume_run(directory, record) is False

    #leftover chunks
    directory, record = mock_state(tmp_path, chunks=3)
    assert _resume_run(directory, record) is True

    directory, record = mock_state(tmp_path, chunks=1)
    assert _resume_run(directory, record) is True

    #no chunks but .done remaining
    directory, record = mock_state(tmp_path, chunks=0, done=3)
    assert _resume_run(directory, record) is False

    #directory is missing
    assert _resume_run(TMP_ROOT / "gone", BASE_RECORD) is False

    #ignored similarly named files
    directory, record = mock_state(tmp_path)
    (directory / "chunk").write_text("", encoding="utf-8")
    (directory / "notes.chunk.csv").write_text("", encoding="utf-8")
    assert _resume_run(directory, record) is False

    #non recursive
    directory, record = mock_state(tmp_path)
    (directory / "nested").mkdir()
    (directory / "nested" / "a.chunk").write_text("", encoding="utf-8")
    assert _resume_run(directory, record) is False

def test_merge_needed():
    tmp_path = TMP_ROOT / "outputs" / "test_merge"
    #no merge on clean directory
    directory, record = mock_state(tmp_path, done=0)
    assert _merge_needed(directory, record) is False

    #merge when done files left
    directory, record = mock_state(tmp_path, done=3)
    assert _merge_needed(directory, record) is True

    #no merge when directory missing
    assert _merge_needed(TMP_ROOT / "gone", BASE_RECORD) is False

def test_status():
    """Tests for overlap in different statuses"""
    tmp_path = TMP_ROOT / "test_status"
    #fresh directory
    directory, record = mock_state(tmp_path, config="match")
    assert _is_new(directory, record) is False
    assert _is_done(directory, record) is False
    assert _resume_run(directory, record) is False
    assert _merge_needed(directory, record) is False

    #need to resume
    directory, record = mock_state(tmp_path, config="match", chunks=2, done=3)
    assert _is_new(directory, record) is False
    assert _is_done(directory, record) is False
    assert _resume_run(directory, record) is True

    #merge needed
    directory, record = mock_state(tmp_path, config="match", chunks=0, done=5)
    assert _is_new(directory, record) is False
    assert _is_done(directory, record) is False
    assert _resume_run(directory, record) is False
    assert _merge_needed(directory, record) is True

    #fully complete
    directory, record = mock_state(tmp_path, config="match", output="full")
    assert _is_done(directory, record) is True
    assert _resume_run(directory, record) is False
    assert _merge_needed(directory, record) is False

    #new and done are mutually exclusive
    for cfg in ["match", None, {"config_hash": "x"}, {"batch_size": 1}]:
        directory, record = mock_state(TMP_ROOT / str(id(cfg)), config=cfg, output="full")
        assert not (_is_new(directory, record) and _is_done(directory, record))


def test_cli():
    """Run the CLI and return the CompletedProcess."""
    args = ["--input", IN, "--output", OUT, "--chunk-size", "--config", "config.yaml", "--batch_size", str(BATCH_SIZE)]
    result = subprocess.run(
        [sys.executable, "fhir transform", *args],
        capture_output=True,
        text=True,
        timeout=60,
    )
    
 
    assert pathlib.Path(OUT).exists(), "CLI exited 0 but wrote no output file"
    records = [orjson.loads(line)
        for line in pathlib.Path(OUT).read_bytes().splitlines()
        if line.strip()]
    in_records = [orjson.loads(line)
        for line in pathlib.Path(IN).read_bytes().splitlines()
        if line.strip()]
    assert len(records) == len(in_records)
    assert all("security" in r.get("meta", {}) for r in records)

@pytest.mark.parametrize("bad", [0, -1, None, "bad"])
def test_invalid_chunk_size_is_rejected(bad):
    out = TMP_ROOT / "cli_test" / "cli_out.ndjson"
    
    args = ["--input", IN, "--output", out, "--chunk-size", "--config", "config.yaml", "--batch_size", str(bad)]
    result = subprocess.run(
            [sys.executable, "fhir transform", *args],
            capture_output=True,
            text=True,
            timeout=60,
        )
  
    assert result.returncode != 0
    assert not pathlib.Path(out).exists(), "rejected the argument but still wrote output"


def test_missing_input_file_fails_cleanly():
    out = TMP_ROOT / "cli_test" / "nope.ndjson"
    args = ["--input", "nope.ndjson", "--output", out, "--chunk-size", "--config", "config.yaml", "--batch_size", str(BATCH_SIZE)]
    result = subprocess.run(
        [sys.executable, "fhir transform", *args],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode != 0
    assert "Traceback" not in result.stderr, "raw traceback instead of an error message"
    assert not pathlib.Path(out).exists()

#depends on whether or not we want the script to create new output directories
def test_output_directory_does_not_exist():
    out = TMP_ROOT / "missing_dir" / "out.ndjson"
    args = ["--input", IN, "--output", out, "--chunk-size", "--config", "config.yaml", "--batch_size", str(BATCH_SIZE)]
    result = subprocess.run(
        [sys.executable, "fhir transform", *args],
        capture_output=True,
        text=True,
        timeout=60,
    )
    
    assert result.returncode != 0
    assert "Traceback" not in result.stderr
