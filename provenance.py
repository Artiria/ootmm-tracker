"""Fingerprints of generator inputs, independent of installation directories."""
import hashlib
from pathlib import Path

# Bump when generator logic changes the meaning of a cached table, even if
# the ROM and its data files stay identical.
CHECKS_REVISION = 2


def rom_digest(data):
    return hashlib.sha256(data).hexdigest()


def file_digest(path):
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def data_digest(root):
    root = Path(root)
    digest = hashlib.sha256()
    # Relative names matter; the random PyInstaller extraction directory does not.
    for path in sorted(p for p in root.rglob("*") if p.is_file()
                       and p.suffix.lower() in (".csv", ".yml", ".yaml", ".xml", ".ts", ".h", ".c")):
        name = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(name).to_bytes(4, "big"))
        digest.update(name)
        digest.update(bytes.fromhex(file_digest(path)))
    return digest.hexdigest()
