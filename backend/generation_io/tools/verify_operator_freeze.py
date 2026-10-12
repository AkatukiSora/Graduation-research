from __future__ import annotations

import hashlib
import json
import stat
import sys
from pathlib import Path


def sha256_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def main() -> int:
    freeze_path = Path(sys.argv[1])
    freeze = json.loads(freeze_path.read_text(encoding="utf-8"))
    refs = [*freeze["input_refs"], freeze["supplement_ref"]]
    mismatches: list[str] = []
    for ref in refs:
        path = Path(ref["path"])
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            mismatches.append(f"missing:{path.name}")
            continue
        if not stat.S_ISREG(mode):
            mismatches.append(f"not_regular:{path.name}")
            continue
        size, digest = sha256_file(path)
        if size != ref["bytes"] or digest != ref["sha256"]:
            mismatches.append(f"hash_or_size_mismatch:{path.name}")

    if mismatches:
        print(json.dumps({"status": "FAIL", "ref_count": len(refs), "mismatches": mismatches}))
        return 2
    print(
        json.dumps(
            {
                "status": "PASS",
                "task_id": freeze["task_id"],
                "base_commit": freeze["base_commit"],
                "ref_count": len(refs),
                "mismatch_count": 0,
                "selected_model": freeze["selected_model"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
