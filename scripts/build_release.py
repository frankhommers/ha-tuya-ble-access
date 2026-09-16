"""Build a deterministic manual-install ZIP containing only the HA integration."""

import hashlib
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "tuya_ble_access"


def main():
    manifest = json.loads((COMPONENT / "manifest.json").read_text())
    version = manifest["version"]
    if not version or any(c not in "0123456789abcdefghijklmnopqrstuvwxyz.-" for c in version):
        raise ValueError("Unexpected release version")
    if (COMPONENT / "LICENSE").read_bytes() != (ROOT / "LICENSE").read_bytes():
        raise ValueError("Integration LICENSE must match the repository LICENSE")
    output = ROOT / "dist"
    output.mkdir(exist_ok=True)
    target = output / f"tuya_ble_access-{version}.zip"
    files = sorted(p for p in COMPONENT.rglob("*")
                   if p.is_file() and (p.suffix in {".py", ".json", ".png", ".yaml"}
                                      or p == COMPONENT / "LICENSE")
                   and "__pycache__" not in p.parts)
    required = {"LICENSE", "__init__.py", "manifest.json", "services.yaml", "strings.json", "translations/en.json", "translations/nl.json"}
    assert required <= {p.relative_to(COMPONENT).as_posix() for p in files}
    with ZipFile(target, "w", compression=ZIP_DEFLATED) as archive:
        for path in files:
            if path.is_symlink():
                raise ValueError("Release files must not be symlinks")
            name = path.relative_to(ROOT).as_posix()
            info = ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes())
    with ZipFile(target) as archive:
        assert archive.testzip() is None
        assert len(archive.namelist()) == len(files)
        for path in files:
            assert archive.read(path.relative_to(ROOT).as_posix()) == path.read_bytes()
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    (output / "SHA256SUMS").write_text(f"{digest}  {target.name}\n")
    print(f"Verified {target.name}: {len(files)} integration files")
    print(f"SHA256: {digest}")


if __name__ == "__main__":
    main()
