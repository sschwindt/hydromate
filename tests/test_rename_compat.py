"""Surviving the hydromate -> aXqua rename.

A rename is easy to do and easy to get subtly wrong: the code all works, and the user's
existing data quietly stops being found. These tests pin the places where that would
happen - an artifact folder holding tens of gigabytes, a job root exported from a shell
profile, a ``profiles.yml`` written last month.

The rule throughout is the same: **the new name is what gets created, the old name is
still read while only it exists.** Nothing here ever overrides a case that has genuinely
been migrated, and nothing here creates anything under the old name.
"""

from __future__ import annotations

import logging

import pytest

from axqua.config import DEFAULT_SIM_DIR, LEGACY_SIM_DIR, load_config
from axqua.jobs import paths

MINIMAL = """
project:
  name: rename-demo
  crs_epsg: 25832
{sim_dir}
telemac:
  pysource: {pysource}
geodata:
  dem_initial: dem.tif
  boundary: roi.gpkg
boundaries:
  liquid_boundaries: liquid.gpkg
  prescribed_flowrate: 2.4
  prescribed_elevation: 100.0
"""


def _case(tmp_path, *, sim_dir: str | None = None, make: str | None = None):
    (tmp_path / "pysource.sh").write_text("# stub\n", encoding="utf-8")
    for name in ("dem.tif", "roi.gpkg", "liquid.gpkg"):
        (tmp_path / name).touch()
    if make:
        (tmp_path / make / "simulation").mkdir(parents=True)
    (tmp_path / "case-config.yml").write_text(
        MINIMAL.format(pysource="pysource.sh",
                       sim_dir=f"  sim_dir: {sim_dir}" if sim_dir else ""),
        encoding="utf-8")
    return load_config(tmp_path / "case-config.yml")


# --------------------------------------------------------------- the artifact folder


def test_a_new_case_gets_the_new_folder(tmp_path):
    cfg = _case(tmp_path)
    assert cfg.model_dir == tmp_path / DEFAULT_SIM_DIR / "simulation"


def test_an_existing_pre_rename_folder_is_still_used(tmp_path, caplog):
    """The case that matters: results built before the rename.

    Pointing at a fresh empty ``axqua-case/`` would present as a case that had never been
    built - and on isar-2025 that is 67 GB of finished work.
    """
    with caplog.at_level(logging.WARNING, logger="axqua"):
        cfg = _case(tmp_path, make=LEGACY_SIM_DIR)
    assert cfg.model_dir == tmp_path / LEGACY_SIM_DIR / "simulation"
    assert "pre-rename" in caplog.text


def test_the_new_folder_wins_once_it_exists(tmp_path):
    """So a migrated case is never dragged back to the old folder."""
    (tmp_path / LEGACY_SIM_DIR / "simulation").mkdir(parents=True)
    cfg = _case(tmp_path, make=DEFAULT_SIM_DIR)
    assert cfg.model_dir == tmp_path / DEFAULT_SIM_DIR / "simulation"


def test_an_explicit_old_folder_is_honoured_verbatim(tmp_path):
    """A config that names the old folder means it, and is left alone."""
    cfg = _case(tmp_path, sim_dir=LEGACY_SIM_DIR, make=LEGACY_SIM_DIR)
    assert cfg.model_dir == tmp_path / LEGACY_SIM_DIR / "simulation"


def test_no_fallback_when_neither_folder_exists(tmp_path):
    """A fresh case must not be handed a path to something that is not there."""
    cfg = _case(tmp_path)
    assert DEFAULT_SIM_DIR in str(cfg.model_dir)


# ------------------------------------------------------------------ the environment


def test_the_pre_rename_environment_variable_still_works(tmp_path, monkeypatch, caplog):
    """A shell profile, cron entry or systemd unit written before the rename still
    exports the old name. Ignoring it would silently relocate someone's jobs."""
    monkeypatch.delenv(paths.ENV_JOB_ROOT, raising=False)
    monkeypatch.setenv("HYDROMATE_JOB_ROOT", str(tmp_path / "oldjobs"))
    paths._warned_legacy.clear()
    with caplog.at_level(logging.WARNING, logger="axqua"):
        assert paths.job_root() == (tmp_path / "oldjobs").resolve()
    assert "pre-rename" in caplog.text


def test_the_new_environment_variable_wins(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.ENV_JOB_ROOT, str(tmp_path / "new"))
    monkeypatch.setenv("HYDROMATE_JOB_ROOT", str(tmp_path / "old"))
    assert paths.job_root() == (tmp_path / "new").resolve()


def test_the_deprecation_notice_is_emitted_once(tmp_path, monkeypatch, caplog):
    """Once, not per call - the job verbs read these on every invocation."""
    monkeypatch.delenv(paths.ENV_JOB_ROOT, raising=False)
    monkeypatch.setenv("HYDROMATE_JOB_ROOT", str(tmp_path))
    paths._warned_legacy.clear()
    with caplog.at_level(logging.WARNING, logger="axqua"):
        for _ in range(5):
            paths.job_root()
    assert caplog.text.count("pre-rename") == 1


# ------------------------------------------------------------- the user directories


def test_a_pre_rename_config_directory_is_still_read(tmp_path, monkeypatch):
    """``profiles.yml`` describes solver installs and is genuinely hand-written; losing
    track of it would mean writing it again."""
    monkeypatch.delenv(paths.ENV_CONFIG, raising=False)
    monkeypatch.delenv("HYDROMATE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    (tmp_path / paths.LEGACY_APP_NAME).mkdir()
    assert paths.config_dir() == tmp_path / paths.LEGACY_APP_NAME


def test_the_new_config_directory_wins_once_it_exists(tmp_path, monkeypatch):
    monkeypatch.delenv(paths.ENV_CONFIG, raising=False)
    monkeypatch.delenv("HYDROMATE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    (tmp_path / paths.LEGACY_APP_NAME).mkdir()
    (tmp_path / paths.APP_NAME).mkdir()
    assert paths.config_dir() == tmp_path / paths.APP_NAME


def test_a_fresh_machine_gets_the_new_directory(tmp_path, monkeypatch):
    monkeypatch.delenv(paths.ENV_DATA, raising=False)
    monkeypatch.delenv("HYDROMATE_DATA_DIR", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert paths.data_dir() == tmp_path / paths.APP_NAME


# --------------------------------------------------------------------- the new name


#: Files where naming the old software is the whole point, exempted wholesale. Listing
#: them beats growing a regex until it exempts everything: a *file* is a decision someone
#: made, whereas a pattern quietly starts matching things nobody intended.
EXEMPT_FILES = {
    "tests/test_rename_compat.py",       # these tests
    "README.md",                         # the migration section
    "CHANGELOG.md",
}

#: Elsewhere, the old name may appear only as part of a named compatibility shim.
SHIM_PATTERN = (r"LEGACY_|legacy|pre-rename|hydromate-case|\.hydromate-prj|HYDROMATE_"
                r"|renam|migrat|formerly")


def test_nothing_ships_the_old_name_any_more():
    """Everything that is not a deliberate compatibility shim.

    Reading the source rather than trusting the substitution: a stray ``hydromate`` in a
    docstring is harmless, but one in a path or an identifier is a bug that only shows up
    on someone else's machine, months later.
    """
    import pathlib
    import re
    import subprocess

    from axqua.jobs import paths as _paths

    root = pathlib.Path(_paths.__file__).parents[3]
    out = subprocess.run(["git", "grep", "-Ini", "hydromate"], cwd=root,
                         capture_output=True, text=True).stdout
    allowed = re.compile(SHIM_PATTERN, re.I)

    stray = []
    for line in out.splitlines():
        if not line.strip():
            continue
        path = line.split(":", 1)[0]
        if path in EXEMPT_FILES or allowed.search(line):
            continue
        stray.append(line)
    assert stray == [], "the old name survives outside the compatibility shims:\n" + \
        "\n".join(stray[:20])


def test_no_tracked_filename_carries_the_old_name():
    """The guard above reads file *contents* and cannot see a filename.

    That gap was real: the plugin icon stayed ``hydromate.svg`` while every reference to
    it had become ``axqua.svg``, so ``metadata.txt`` named an icon that was not there and
    the plugin would have shipped without one. The zip validator caught it; this makes
    sure the test suite does too.
    """
    import pathlib
    import subprocess

    from axqua.jobs import paths as _paths

    root = pathlib.Path(_paths.__file__).parents[3]
    tracked = subprocess.run(["git", "ls-files"], cwd=root,
                             capture_output=True, text=True).stdout.splitlines()
    offenders = [name for name in tracked if "hydromate" in name.lower()]
    assert offenders == [], f"these filenames still carry the old name: {offenders}"


def test_the_plugin_package_is_publishable():
    """Runs the same validation the release workflow does, so a packaging problem fails
    here rather than at a tag."""
    import pathlib
    import subprocess
    import sys

    from axqua.jobs import paths as _paths

    root = pathlib.Path(_paths.__file__).parents[3]
    proc = subprocess.run([sys.executable, "scripts/build_plugin_zip.py", "--check"],
                          cwd=root, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_the_readme_actually_documents_the_migration():
    """README.md is exempted above, so this makes sure it earns the exemption."""
    import pathlib

    from axqua.jobs import paths as _paths

    readme = (pathlib.Path(_paths.__file__).parents[3] / "README.md").read_text("utf-8")
    assert "Migrating from hydromate" in readme
    for expected in ("pip install aXqua", "import axqua", "axqua-case",
                     "AXQUA_JOB_ROOT", ".axqua-prj"):
        assert expected in readme, f"the migration table does not mention {expected}"


def test_the_package_imports_under_the_new_name():
    import axqua
    from axqua.jobs import JobState          # noqa: F401 - the lazy export path
    assert axqua.__version__


def test_the_distribution_is_branded_but_the_module_is_lowercase():
    """``pip install aXqua`` and ``import axqua``. PyPI normalises the two spellings to
    one project, so the brand survives without a mixed-case module name."""
    from importlib.metadata import metadata

    assert metadata("aXqua")["Name"].lower() == "axqua"
    import axqua
    assert axqua.__name__ == "axqua"


@pytest.mark.parametrize("path", ["src/axqua", "qgis_plugin/axqua"])
def test_the_directories_were_moved_not_copied(path):
    import pathlib

    from axqua.jobs import paths as _paths
    root = pathlib.Path(_paths.__file__).parents[3]
    assert (root / path).is_dir()
    assert not (root / path.replace("axqua", "hydromate")).exists()
