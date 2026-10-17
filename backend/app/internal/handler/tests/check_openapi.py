#!/usr/bin/env python3
"""Check that backend/docs/openapi.yaml matches the real handler output.

The script runs the handler contract test, which writes the JSON the handlers
actually produce, and compares property names, required fields, nullability and
error codes with the OpenAPI document. Examples inside the document are checked
against their schemas too. No database is needed.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    import yaml

    def load_yaml(text):
        return yaml.safe_load(text)
except ModuleNotFoundError:  # fall back to ruamel.yaml when PyYAML is absent
    from ruamel.yaml import YAML

    def load_yaml(text):
        return YAML(typ="safe").load(text)

APP_DIR = Path(__file__).resolve().parents[3]
OPENAPI = APP_DIR.parent / "docs" / "openapi.yaml"


def go_env(report):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/tmp")}
    for key in ("GOTOOLCHAIN", "GOCACHE", "GOPATH"):
        env[key] = report["environment"][key]
    return env


def collect_samples(report):
    with tempfile.TemporaryDirectory() as scratch:
        path = Path(scratch) / "samples.json"
        env = dict(go_env(report), API_CONTRACT_SAMPLE_PATH=str(path))
        result = subprocess.run(
            [report["go_binary"], "test", "./internal/handler", "-run", "TestContractSamples", "-count=1"],
            env=env, cwd=APP_DIR, capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0 or not path.exists():
            sys.exit(f"contract sample generation failed:\n{result.stdout}{result.stderr}")
        return json.loads(path.read_text())


def props(schemas, name):
    schema = schemas[name]
    found = dict(schema.get("properties", {}))
    required = set(schema.get("required", []))
    for part in schema.get("allOf", []):
        if "$ref" in part:
            inner_found, inner_required = props(schemas, part["$ref"].rsplit("/", 1)[1])
        else:
            inner_found, inner_required = dict(part.get("properties", {})), set(part.get("required", []))
        found.update(inner_found)
        required |= inner_required
    return found, required


def check_object(label, value, properties, required, problems):
    keys = set(value)
    if keys != set(properties):
        problems.append(f"{label}: keys {sorted(keys)} differ from schema properties {sorted(properties)}")
    if not required <= keys:
        problems.append(f"{label}: required fields missing: {sorted(required - keys)}")
    for key, spec in properties.items():
        if key not in value:
            continue
        item = value[key]
        if item is None:
            if not spec.get("nullable"):
                problems.append(f"{label}.{key}: null but the schema is not nullable")
            continue
        if "enum" in spec and item not in spec["enum"]:
            problems.append(f"{label}.{key}: {item!r} not in {spec['enum']}")
        expected = {"string": str, "boolean": bool}.get(spec.get("type"))
        if expected and not isinstance(item, expected):
            problems.append(f"{label}.{key}: expected {spec['type']}, got {type(item).__name__}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--toolchain-report", required=True, type=Path)
    args = parser.parse_args()
    report = json.loads(args.toolchain_report.read_text())
    document = load_yaml(OPENAPI.read_text())
    schemas = document["components"]["schemas"]
    samples = collect_samples(report)
    problems = []

    item_props, item_required = props(schemas, "StopPointRangeResponseItem")
    detail_props, detail_required = props(schemas, "StopPointDetailResponse")
    health_props, health_required = props(schemas, "HealthResponse")
    error_props, error_required = props(schemas, "ErrorBody")

    for index, item in enumerate(samples["range"]):
        check_object(f"range[{index}]", item, item_props, item_required, problems)
    if [item["az"] for item in samples["range"]] != [None, "197.5"]:
        problems.append("range samples must cover a null and a decimal-string az")
    check_object("detail", samples["detail"], detail_props, detail_required, problems)
    check_object("health_ok", samples["health_ok"], health_props, health_required, problems)
    check_object("health_degraded", samples["health_degraded"], health_props, health_required, problems)

    emitted_codes = set()
    for name, sample in samples.items():
        if name.startswith("error_"):
            check_object(name, sample["error"], error_props, error_required, problems)
            emitted_codes.add(sample["error"]["code"])
    documented_codes = set(error_props["code"]["enum"])
    if emitted_codes != documented_codes:
        problems.append(f"error codes: handlers emit {sorted(emitted_codes)}, document lists {sorted(documented_codes)}")

    # Every inline example must itself follow its schema.
    examples = {
        ("/api/v1/stop-points", "200"): (item_props, item_required, True),
        ("/api/v1/stop-points/{id}", "200"): (detail_props, detail_required, False),
        ("/api/v1/health", "200"): (health_props, health_required, False),
        ("/api/v1/health", "503"): (health_props, health_required, False),
    }
    for (path, status), (properties, required, is_list) in examples.items():
        content = document["paths"][path]["get"]["responses"][status]["content"]["application/json"]
        for name, example in content["examples"].items():
            for index, value in enumerate(example["value"] if is_list else [example["value"]]):
                check_object(f"example {path} {status} {name}[{index}]", value, properties, required, problems)
    for path, status in (("/api/v1/stop-points", "400"), ("/api/v1/stop-points", "503"), ("/api/v1/stop-points/{id}", "404"),
                         ("/api/v1/stop-points/{id}", "500"), ("/api/v1/stop-points/{id}", "503")):
        content = document["paths"][path]["get"]["responses"][status]["content"]["application/json"]
        for name, example in content["examples"].items():
            check_object(f"example {path} {status} {name}", example["value"]["error"], error_props, error_required, problems)

    if problems:
        print("\n".join(problems))
        sys.exit(1)
    print(json.dumps({"openapi_matches_handlers": True, "range_keys": sorted(item_props), "error_codes": sorted(documented_codes)}))


if __name__ == "__main__":
    main()
