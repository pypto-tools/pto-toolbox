"""Build a client-only archive inside the native manylinux_2_28 container."""

import importlib.metadata
import json
import os
import platform
import shutil
import subprocess
import sys
import sysconfig
import tarfile
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1]
MODULES = ("__init__.py", "cli.py", "aliases.py", "client_config.py", "version.py")
RUNTIME = ("httpx", "httpcore", "h11", "anyio", "certifi", "idna", "typing-extensions")


def main():
    if os.confstr("CS_GNU_LIBC_VERSION") != "glibc 2.28":
        raise SystemExit("Build inside the manylinux_2_28 image to preserve the glibc baseline")
    machine = platform.machine()
    if machine not in {"aarch64", "x86_64"}:
        raise SystemExit("Unsupported build architecture")
    build = SERVICE / "build" / ("client-" + machine)
    if build.exists():
        shutil.rmtree(build)
    source = build / "source"
    package = source / "pr_rerun"
    package.mkdir(parents=True)
    for module in MODULES:
        shutil.copyfile(SERVICE / "src" / "pr_rerun" / module, package / module)
    for module in ("client_entry.py", "client_install.py"):
        shutil.copyfile(SERVICE / "release" / module, source / module)
    # Only the allowlisted source tree is visible to the bundler.
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--clean",
            "--noconfirm",
            "--onedir",
            "--name",
            "pr-rerun",
            "--paths",
            str(source),
            "--specpath",
            str(build),
            "--workpath",
            str(build / "work"),
            "--distpath",
            str(build / "dist"),
            str(source / "client_entry.py"),
        ],
        cwd=source,
        check=True,
    )
    output = build / "dist" / "pr-rerun"
    version = (
        subprocess.check_output([str(output / "pr-rerun"), "--version"], text=True)
        .strip()
        .split()[-1]
    )
    # Refuse source/tag drift before any release is published.
    tag = os.environ.get("RELEASE_TAG")
    if tag and tag != "pr-rerun-v" + version:
        raise SystemExit("Release tag does not match the client version")
    dependencies = {name: importlib.metadata.version(name) for name in RUNTIME}
    (output / "release.json").write_text(
        json.dumps(
            {
                "format": 1,
                "version": version,
                "architecture": machine,
                "glibc_min": "2.28",
                "python": platform.python_version(),
                "commit": os.environ.get("GITHUB_SHA", "local"),
                "dependencies": dependencies,
            },
            indent=2,
        )
        + "\n"
    )
    licenses = output / "licenses"
    licenses.mkdir()
    python_licenses = Path(sys.base_prefix) / "licenses"
    if python_licenses.is_dir():
        shutil.copytree(python_licenses, licenses / "python-runtime")
    candidates = (
        Path(sysconfig.get_path("stdlib")) / "LICENSE.txt",
        Path(sys.base_prefix) / "LICENSE.txt",
        python_licenses / "LICENSE.cpython.txt",
    )
    python_license = next((path for path in candidates if path.is_file()), None)
    if python_license is None:
        raise SystemExit("The Python distribution must include its license")
    shutil.copyfile(python_license, licenses / "Python.txt")
    for name in RUNTIME:
        distribution = importlib.metadata.distribution(name)
        found = False
        for item in distribution.files or []:
            if "license" in item.name.lower() or "copying" in item.name.lower():
                source_file = Path(distribution.locate_file(item))
                if source_file.is_file():
                    target = licenses / name / str(item)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source_file, target)
                    found = True
        if not found:
            raise SystemExit("No license found for " + name)
    destination = SERVICE / "dist"
    destination.mkdir(exist_ok=True)
    archive = destination / ("pr-rerun-client-linux-" + machine + ".tar.gz")
    # Flatten PyInstaller symlinks; the installer accepts only regular files/directories.
    with tarfile.open(archive, "w:gz", dereference=True) as bundle:
        bundle.add(output, arcname="pr-rerun-client")
    print(archive)


if __name__ == "__main__":
    main()
