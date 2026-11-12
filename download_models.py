"""Download public model weights used by the local cabin speech engine."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tarfile
import urllib.request
from pathlib import Path


RELEASE_ROOT = "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
SPEAKER_NAME = "nemo_en_titanet_small.onnx"
SEGMENTATION_ARCHIVE = "sherpa-onnx-pyannote-segmentation-3-0.tar.bz2"


def download(url: str, destination: Path):
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "CabinConversationModelSetup/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=120) as response, temporary.open("wb") as output:
            expected = response.headers.get("Content-Length")
            shutil.copyfileobj(response, output, length=1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        size = temporary.stat().st_size
        if size < 1024:
            raise RuntimeError(f"Model download is unexpectedly small: {destination.name}")
        if expected and size != int(expected):
            raise RuntimeError(f"Incomplete model download: {destination.name}")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def sha256(path: Path):
    checksum = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def download_models(model_dir: Path):
    from huggingface_hub import snapshot_download

    model_dir.mkdir(parents=True, exist_ok=True)
    print("Downloading the public multilingual Whisper small model...", flush=True)
    snapshot_download(
        repo_id="Systran/faster-whisper-small",
        local_dir=str(model_dir / "whisper-small"),
        allow_patterns=["config.json", "model.bin", "tokenizer.json", "vocabulary.txt"],
        token=False,
    )
    speaker_path = model_dir / SPEAKER_NAME
    speaker_url = RELEASE_ROOT + "speaker-recongition-models/" + SPEAKER_NAME
    if not speaker_path.is_file() or speaker_path.stat().st_size < 1024 * 1024:
        print("Downloading speaker embeddings...", flush=True)
        download(speaker_url, speaker_path)
    segmentation_path = model_dir / "segmentation.onnx"
    archive_url = RELEASE_ROOT + "speaker-segmentation-models/" + SEGMENTATION_ARCHIVE
    if not segmentation_path.is_file() or segmentation_path.stat().st_size < 1024 * 1024:
        archive_path = model_dir / SEGMENTATION_ARCHIVE
        print("Downloading speech segmentation...", flush=True)
        download(archive_url, archive_path)
        temporary = segmentation_path.with_name(segmentation_path.name + ".part")
        try:
            with tarfile.open(archive_path, "r:bz2") as archive:
                model_member = archive.getmember("sherpa-onnx-pyannote-segmentation-3-0/model.onnx")
                if not model_member.isfile() or not 1024 < model_member.size < 200 * 1024 * 1024:
                    raise RuntimeError("Unexpected segmentation model archive member")
                # Only this expected regular file is copied. No paths or links are extracted.
                with archive.extractfile(model_member) as source, temporary.open("wb") as output:
                    shutil.copyfileobj(source, output)
                if temporary.stat().st_size != model_member.size:
                    raise RuntimeError("Incomplete segmentation model extraction")
            os.replace(temporary, segmentation_path)
        finally:
            temporary.unlink(missing_ok=True)
            archive_path.unlink(missing_ok=True)
    manifest = {
        "whisper": "https://huggingface.co/Systran/faster-whisper-small",
        "speaker": {"source": speaker_url, "sha256": sha256(speaker_path)},
        "segmentation": {"source": archive_url, "sha256": sha256(segmentation_path)},
    }
    (model_dir / "sources.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print("All local speech models are ready.", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", type=Path, default=Path(__file__).parent / "models")
    arguments = parser.parse_args()
    download_models(arguments.model_dir.resolve())
