#!/usr/bin/env python3

import argparse
import re
import tarfile
import urllib.request
from pathlib import Path

import yaml


ARROW_MODULE_NAME = "arrow"
ARROW_VERSIONS_MEMBER = "cpp/thirdparty/versions.txt"


def load_manifest(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read().splitlines(keepends=True)


def get_arrow_archive_url(manifest_path):
    with open(manifest_path, "r", encoding="utf-8") as handle:
        manifest = yaml.safe_load(handle)

    for module in manifest.get("modules", []):
        if not isinstance(module, dict):
            continue
        if module.get("name") != ARROW_MODULE_NAME:
            continue
        for source in module.get("sources", []):
            if source.get("type") == "archive" and source.get("url"):
                return source["url"]
    raise SystemExit(f"could not find {ARROW_MODULE_NAME!r} archive URL in {manifest_path}")


def extract_versions_txt(archive_url):
    with urllib.request.urlopen(archive_url) as response, tarfile.open(
        fileobj=response, mode="r|*"
    ) as archive:
        for member in archive:
            if member.isfile() and member.name.endswith(ARROW_VERSIONS_MEMBER):
                extracted = archive.extractfile(member)
                if extracted is None:
                    break
                return extracted.read().decode("utf-8")
    raise SystemExit(f"could not find {ARROW_VERSIONS_MEMBER} in {archive_url}")


def parse_versions_txt(content):
    variables = {}
    dependencies = []
    in_dependencies = False

    for line in content.splitlines():
        if line.startswith("ARROW_") and "_BUILD_VERSION=" in line:
            key, value = line.split("=", 1)
            variables[key] = value
        elif line.startswith("ARROW_") and "_BUILD_SHA256_CHECKSUM=" in line:
            key, value = line.split("=", 1)
            variables[key] = value
        elif line.startswith("DEPENDENCIES="):
            in_dependencies = True
        elif in_dependencies:
            if line.strip() == ")":
                in_dependencies = False
                continue
            match = re.match(r'\s*"(?P<url_var>ARROW_[A-Z0-9_]+_URL)\s+(?P<tarball>\S+)\s+(?P<url>\S+)"\s*$', line)
            if match:
                dependencies.append(
                    (
                        match.group("url_var"),
                        match.group("tarball"),
                        match.group("url"),
                    )
                )
    return variables, dependencies


def expand_template(template, variables):
    def replace(match):
        expr = match.group(1)
        if "//./_" in expr:
            var_name = expr.split("//./_", 1)[0]
            return variables[var_name].replace(".", "_")
        if ":" in expr:
            var_name, offset = expr.split(":", 1)
            return variables[var_name][int(offset) :]
        return variables[expr]

    return re.sub(r"\$\{([^}]+)\}", replace, template)


def render_dependency_env_lines(variables, dependencies):
    lines = []
    for url_var, tarball, url in dependencies:
        build_var = url_var.replace("_URL", "_BUILD_SHA256_CHECKSUM")
        if build_var not in variables:
            raise SystemExit(f"missing checksum for {url_var} in Arrow versions.txt")
        lines.append(
            f"        {url_var}: /run/build/arrow/cpp/thirdparty/{expand_template(tarball, variables)}\n"
        )
    return lines


def render_dependency_sources(variables, dependencies):
    blocks = []
    for url_var, tarball, url in dependencies:
        build_var = url_var.replace("_URL", "_BUILD_SHA256_CHECKSUM")
        blocks.append(
            "      - type: file\n"
            f"        url: {expand_template(url, variables)}\n"
            f"        sha256: {variables[build_var]}\n"
            "        dest: cpp/thirdparty\n"
            f"        dest-filename: {expand_template(tarball, variables)}\n"
        )
    return blocks


def update_manifest(manifest_lines, dependency_env_lines, dependency_source_blocks):
    updated = []
    in_arrow_module = False
    env_inserted = False
    files_inserted = False

    for line in manifest_lines:
        if line.startswith("  - name: "):
            in_arrow_module = line.strip() == f"- name: {ARROW_MODULE_NAME}"
            env_inserted = False
            files_inserted = False
            updated.append(line)
            continue

        if not in_arrow_module:
            updated.append(line)
            continue

        if re.match(r"^\s{8}ARROW_[A-Z0-9_]+_URL:\s+", line):
            if not env_inserted:
                updated.extend(dependency_env_lines)
                env_inserted = True
            continue

        if line == "      - type: file\n":
            if not files_inserted:
                updated.extend(dependency_source_blocks)
                files_inserted = True
            continue

        if files_inserted and line.startswith("  - name: "):
            # Handled at the top of the loop on the next iteration.
            updated.append(line)
            continue

        if files_inserted and line.strip() == "":
            # Skip old file-source blank padding.
            continue

        updated.append(line)

    return updated


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "versions_file",
        nargs="?",
        help="kept for compatibility; the manifest now drives the Arrow version",
    )
    parser.add_argument(
        "manifest",
        nargs="?",
        default="org.eclipse.sumo.yaml",
        help="Flatpak manifest to update",
    )
    args = parser.parse_args()

    manifest_path = Path(args.manifest)
    manifest_lines = load_manifest(manifest_path)
    archive_url = get_arrow_archive_url(manifest_path)
    versions_txt = extract_versions_txt(archive_url)
    variables, dependencies = parse_versions_txt(versions_txt)
    dependency_env_lines = render_dependency_env_lines(variables, dependencies)
    dependency_source_blocks = render_dependency_sources(variables, dependencies)

    updated_lines = update_manifest(
        manifest_lines, dependency_env_lines, dependency_source_blocks
    )

    with open(manifest_path, "w", encoding="utf-8") as handle:
        handle.writelines(updated_lines)


if __name__ == "__main__":
    main()
