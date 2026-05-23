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


def build_dependency_data(variables, dependencies):
    data = []
    for url_var, tarball, url in dependencies:
        build_var = url_var.replace("_URL", "_BUILD_SHA256_CHECKSUM")
        if build_var not in variables:
            raise SystemExit(f"missing checksum for {url_var} in Arrow versions.txt")
        data.append(
            {
                "url_var": url_var,
                "url": expand_template(url, variables),
                "sha256": variables[build_var],
                "tarball": expand_template(tarball, variables),
            }
        )
    return data


def update_manifest(manifest_lines, dependency_data):
    updated = []
    in_arrow_module = False
    env_index = 0
    file_index = 0
    file_block_active = False

    for line in manifest_lines:
        if line.startswith("  - name: "):
            next_is_arrow = line.strip() == f"- name: {ARROW_MODULE_NAME}"
            if in_arrow_module and not next_is_arrow:
                if env_index != len(dependency_data) or file_index != len(dependency_data):
                    raise SystemExit("not all Arrow dependencies were updated from versions.txt")
            in_arrow_module = next_is_arrow
            if in_arrow_module:
                env_index = 0
                file_index = 0
                file_block_active = False
            updated.append(line)
            continue

        if not in_arrow_module:
            updated.append(line)
            continue

        if env_index == len(dependency_data) and file_index == len(dependency_data):
            continue

        if line == "      - type: file\n":
            if file_index >= len(dependency_data):
                raise SystemExit("found more Arrow file sources in the manifest than in versions.txt")
            updated.append(line)
            file_block_active = True
            continue

        if file_block_active:
            current = dependency_data[file_index]
            if re.match(r"^\s{8}url:\s+", line):
                updated.append(f"        url: {current['url']}\n")
                continue
            if re.match(r"^\s{8}sha256:\s+", line):
                updated.append(f"        sha256: {current['sha256']}\n")
                continue
            if re.match(r"^\s{8}dest-filename:\s+", line):
                updated.append(f"        dest-filename: {current['tarball']}\n")
                file_index += 1
                file_block_active = False
                continue

        match = re.match(r"^\s{8}(ARROW_[A-Z0-9_]+_URL):\s+", line)
        if match:
            if env_index >= len(dependency_data):
                raise SystemExit("found more Arrow env vars in the manifest than in versions.txt")
            current = dependency_data[env_index]
            if current["url_var"] != match.group(1):
                raise SystemExit(
                    f"Arrow dependency order mismatch: manifest has {match.group(1)} "
                    f"but versions.txt has {current['url_var']}"
                )
            updated.append(
                f"        {current['url_var']}: /run/build/arrow/cpp/thirdparty/{current['tarball']}\n"
            )
            env_index += 1
            continue

        updated.append(line)

    if in_arrow_module and (
        env_index != len(dependency_data) or file_index != len(dependency_data)
    ):
        raise SystemExit("not all Arrow dependencies were updated from versions.txt")

    return updated


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "versions_file",
        nargs="?",
        help="kept for compatibility; the manifest provides the Arrow version",
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
    dependency_data = build_dependency_data(variables, dependencies)

    updated_lines = update_manifest(manifest_lines, dependency_data)

    with open(manifest_path, "w", encoding="utf-8") as handle:
        handle.writelines(updated_lines)


if __name__ == "__main__":
    main()
