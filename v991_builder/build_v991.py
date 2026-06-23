#!/usr/bin/env python3
from __future__ import annotations
import base64, gzip, hashlib, subprocess, tempfile, zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
BASE = ROOT / "coinbot_update_v989.zip"
OUT = ROOT / "coinbot_update_v991.zip"
OUTPUT = [
    "coinbot_main_v2_13_991.py", "paper_bot_v0.966.py", "DEPLOY_BUNDLE.json",
    "DEPLOY_TARGET.txt", "V991_RELEASE_NOTES.txt", "APPLY_AFTER_v991.txt",
    "V991_LOCAL_VERIFY.txt", "FILES_SHA256_v991.tsv",
]

def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def restore_patch(prefix: str, destination: Path) -> None:
    encoded = "".join(
        p.read_text(encoding="ascii").strip()
        for p in sorted((HERE / "chunks").glob(f"{prefix}_*.b64"))
    )
    if not encoded:
        raise RuntimeError(f"missing payload chunks: {prefix}")
    destination.write_bytes(gzip.decompress(base64.b64decode(encoded)))

def main() -> int:
    if not BASE.is_file():
        raise FileNotFoundError(BASE)
    with tempfile.TemporaryDirectory(prefix="v991-") as temp_dir:
        temp = Path(temp_dir)
        subprocess.run(["unzip", "-q", str(BASE), "-d", str(temp)], check=True)
        restore_patch("main", temp / "main.patch")
        restore_patch("paper", temp / "paper.patch")
        subprocess.run(["patch", "--batch", "--forward", "-p0", "-i", str(temp / "main.patch")], cwd=temp, check=True)
        subprocess.run(["patch", "--batch", "--forward", "-p0", "-i", str(temp / "paper.patch")], cwd=temp, check=True)
        (temp / "coinbot_main_v2_13_989.py").rename(temp / "coinbot_main_v2_13_991.py")
        (temp / "paper_bot_v0.965.py").rename(temp / "paper_bot_v0.966.py")
        for old in ["APPLY_AFTER_v989.txt", "FILES_SHA256_v989.tsv", "V989_LOCAL_VERIFY.txt", "V989_RELEASE_NOTES.txt"]:
            (temp / old).unlink(missing_ok=True)
        for name in OUTPUT[2:]:
            (temp / name).write_bytes((HERE / name).read_bytes())
        expected = {}
        for line in (temp / "FILES_SHA256_v991.tsv").read_text(encoding="utf-8").splitlines():
            parts = line.split("\t")
            if len(parts) == 2 and len(parts[0]) == 64:
                expected[parts[1]] = parts[0]
        for name in ["coinbot_main_v2_13_991.py", "paper_bot_v0.966.py"]:
            actual = sha256(temp / name)
            if expected.get(name) != actual:
                raise RuntimeError(f"hash mismatch {name}: {actual}")
        OUT.unlink(missing_ok=True)
        with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for name in OUTPUT:
                archive.write(temp / name, name)
    with zipfile.ZipFile(OUT, "r") as archive:
        bad = archive.testzip()
        if bad:
            raise RuntimeError(f"bad zip member: {bad}")
        if archive.namelist() != OUTPUT:
            raise RuntimeError(f"unexpected members: {archive.namelist()}")
    print(f"built {OUT.name} size={OUT.stat().st_size} sha256={sha256(OUT)}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
