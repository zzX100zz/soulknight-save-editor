"""Device-side flow: pull the save, edit it locally, push it back, verify.

Two properties of AirLift shape everything here:

* a write can only create a path that is currently *absent*, so replacing a file
  always means "move it out, then write a new one";
* only Media can be read back, so a file must be moved out to be inspected.

That is why the container is empty while a job runs.  Every step therefore keeps
a local copy of what was pulled, and the original is also stashed inside the
device's Media folder, so an interrupted run can always be recovered.
"""

from __future__ import annotations

import hashlib
import posixpath
import shutil
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

from . import device as dev
from . import fields
from .device import SkError
from .patcher import UnlockOptions, apply as apply_unlocks, describe_state
from .workspace import SaveWorkspace

Log = Callable[[str], None]
STASH_PREFIX = "sksave-backup-"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def tree_digest(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): _sha256(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


@dataclass
class Device:
    udid: str
    name: str = ""
    version: str = ""
    container: str = ""

    @property
    def documents(self) -> str:
        return f"{self.container}/Documents"

    @property
    def prefs(self) -> str:
        return f"{self.container}/Library/Preferences/{fields.PREFS_NAME}"


def connect(
    udid: str | None = None,
    container: str | None = None,
    *,
    bundle_id: str = fields.BUNDLE_ID,
    log: Log = lambda message: None,
    container_cache: Path | None = None,
) -> Device:
    """Resolve the paired iPhone and the app's data container."""
    entry = dev.resolve(udid)
    dev.configure(udid=entry["udid"], container=container)
    log(f"device: {entry['name']} ({entry['product']}, iOS {entry['version']}, {entry['transport']})")
    path = dev.find_container(entry["udid"], bundle_id, explicit=container, cache=container_cache)
    if not container and container_cache is not None and container_cache.is_file():
        if container_cache.read_text().strip() == path:
            log("container: remembered from the last run (used because the iPhone is locked)")
    dev.configure(udid=entry["udid"], container=path)
    log(f"container: {path}")
    return Device(
        udid=entry["udid"],
        name=entry["name"],
        version=entry["version"],
        container=path,
    )


@dataclass
class SaveSession:
    device: Device
    workdir: Path
    log: Log = lambda message: None
    verify: bool = True
    retries: int = 3
    state: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------ #
    # paths
    # ------------------------------------------------------------------ #
    @property
    def live(self) -> Path:
        return self.workdir / "live"

    @property
    def backups(self) -> Path:
        return self.workdir / "backups"

    def _retry(self, action: Log, description: str):
        """AirLift calls occasionally lose a helper response; retry with backoff."""
        last: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                return action()
            except Exception as error:  # noqa: BLE001
                last = error
                if attempt < self.retries:
                    self.log(f"{description}: {error} (retry {attempt}/{self.retries - 1})")
                    time.sleep(3 * attempt)
        raise RuntimeError(f"{description} failed after {self.retries} attempts: {last}")

    # ------------------------------------------------------------------ #
    # pull / push
    # ------------------------------------------------------------------ #
    def refresh_container(self) -> str | None:
        """Ask the device for the container again, ignoring the remembered copy."""
        try:
            return dev.find_container(self.device.udid, fields.BUNDLE_ID, cache=None)
        except Exception:  # noqa: BLE001 - still locked, or the device went away
            return None

    def _pull_one(self, remote: str, local: Path, description: str) -> None:
        """Pull one path, recovering once if the remembered container was stale."""
        try:
            self._retry(lambda: dev.pull(self.device.udid, remote, local), description)
            return
        except Exception as error:  # noqa: BLE001
            found = self.refresh_container()
            if found and found != self.device.container:
                self.log(f"{description}: the container moved to {found}, retrying there")
                suffix = remote[len(self.device.container):]
                self.device = replace(self.device, container=found)
                self._retry(lambda: dev.pull(self.device.udid, found + suffix, local), description)
                return
            raise SkError(
                f"{description} failed: the app's data could not be moved out of the container.\n"
                "  - Unlock the iPhone and keep it connected.  iOS stops handing over app data "
                "once the phone has been locked for a while, even when everything else is fine.\n"
                "  - If the game was reinstalled or updated, its container path has changed: "
                "unlock the phone and run once more so the new path can be discovered.\n"
                "  - Last resort: pass the path yourself, "
                "--container /var/mobile/Containers/Data/Application/<UUID>"
            ) from error

    def pull(self, *, into: Path | None = None, with_prefs: bool = True) -> Path:
        """Move the save off the phone into ``workdir/live``."""
        target = Path(into) if into else self.live
        shutil.rmtree(target, ignore_errors=True)
        target.mkdir(parents=True, exist_ok=True)
        self._pull_one(self.device.documents, target / fields.DOCUMENTS, "pull Documents")
        self.log(f"pulled {target / fields.DOCUMENTS}")
        if with_prefs:
            try:
                self._pull_one(self.device.prefs, target / "prefs.plist", "pull prefs")
                self.log("pulled prefs.plist")
            except Exception as error:  # noqa: BLE001
                self.log(f"warning: could not pull prefs.plist ({error})")
        self.state["pulledTo"] = str(target)
        return target

    def push(self, source: Path, *, with_prefs: bool = True) -> dict[str, Any]:
        """Write ``source`` (a live-style directory) back into the container."""
        documents = Path(source) / fields.DOCUMENTS
        if not documents.is_dir():
            raise RuntimeError(f"{documents} is missing - nothing to push")
        result: dict[str, Any] = {}
        self._retry(
            lambda: dev.push_tree(self.device.udid, documents, self.device.documents),
            "push Documents",
        )
        result["documents"] = True
        self.log(f"pushed {len(list(documents.iterdir()))} entries back to Documents")
        prefs = Path(source) / "prefs.plist"
        if with_prefs and prefs.is_file():
            self._retry(
                lambda: dev.push(self.device.udid, prefs, self.device.prefs),
                "push prefs",
            )
            result["prefs"] = True
            self.log("pushed prefs.plist")
        return result

    # ------------------------------------------------------------------ #
    # backups
    # ------------------------------------------------------------------ #
    def backup(self, source: Path, label: str = "") -> Path:
        """Keep a local copy, and stash a second copy inside the device's Media."""
        stamp = time.strftime("%Y%m%d-%H%M%S")
        destination = self.backups / (label or stamp)
        destination.mkdir(parents=True, exist_ok=True)
        shutil.copytree(Path(source) / fields.DOCUMENTS, destination / fields.DOCUMENTS, dirs_exist_ok=True)
        prefs = Path(source) / "prefs.plist"
        if prefs.is_file():
            shutil.copy2(prefs, destination / "prefs.plist")
        self.log(f"local backup: {destination}")
        self.state.setdefault("backups", []).append(str(destination))
        return destination

    def stash_on_device(self, source: Path, label: str = "") -> str:
        """Copy a backup into /var/mobile/Media so it survives on the phone."""
        stamp = time.strftime("%Y%m%d-%H%M%S")
        name = f"{STASH_PREFIX}{label}{'-' if label else ''}{stamp}"
        remote = posixpath.join(dev.MEDIA_ROOT, name)
        try:
            dev.media_mkdir(remote, self.device.udid)
            dev.media_push_tree(Path(source) / fields.DOCUMENTS, posixpath.join(remote, "Documents"), self.device.udid)
            prefs = Path(source) / "prefs.plist"
            if prefs.is_file():
                dev.media_write(posixpath.join(remote, "prefs.plist"), prefs.read_bytes(), self.device.udid)
            self.log(f"device backup: {remote}")
            self.state.setdefault("deviceBackups", []).append(remote)
            return remote
        except Exception as error:  # noqa: BLE001
            self.log(f"warning: device-side backup failed ({error})")
            return ""

    def device_backups(self) -> list[str]:
        """Stashes that are currently sitting in the phone's Media folder."""
        try:
            rows = dev.media_list(dev.MEDIA_ROOT, depth=1, udid=self.device.udid)
        except Exception:  # noqa: BLE001
            return []
        return sorted(
            row["path"]
            for row in rows
            if posixpath.basename(row.get("path", "")).startswith(STASH_PREFIX)
        )

    def restore_from_device(self, remote: str) -> dict[str, Any]:
        """Recover using a stash stored in Media (works even if the container is empty)."""
        staging = self.workdir / "restore-staging"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)
        dev.media_pull_tree(posixpath.join(remote, "Documents"), staging / fields.DOCUMENTS, self.device.udid)
        prefs = posixpath.join(remote, "prefs.plist")
        blob = dev.media_read(prefs, self.device.udid)
        if blob:
            (staging / "prefs.plist").write_bytes(blob)
        return self.push(staging, with_prefs=bool(blob))

    # ------------------------------------------------------------------ #
    # the actual job
    # ------------------------------------------------------------------ #
    def stash_legacy_files(self, source: Path) -> list[str]:
        """Move the unsupported ``.data.new`` / ``.data.rij`` files aside.

        They stay in the local backup, they are just not pushed back, so the game
        falls back to the classic shards this editor understands.
        """
        documents = Path(source) / fields.DOCUMENTS
        aside = Path(source) / "legacy-new"
        aside.mkdir(parents=True, exist_ok=True)
        moved: list[str] = []
        for path in sorted(documents.iterdir()):
            if path.name.endswith(fields.LEGACY_FORMAT_SUFFIXES):
                shutil.move(str(path), str(aside / path.name))
                moved.append(path.name)
        if moved:
            self.log(f"moved {len(moved)} new-format files aside ({', '.join(moved[:3])}...)")
        return moved

    def readback_check(self, expected: Path) -> dict[str, Any]:
        """Move the container back out and compare it with what we pushed."""
        staged = self.workdir / "readback"
        self.pull(into=staged, with_prefs=False)
        want = tree_digest(Path(expected) / fields.DOCUMENTS)
        got = tree_digest(staged / fields.DOCUMENTS)
        missing = sorted(set(want) - set(got))
        extra = sorted(set(got) - set(want))
        mismatched = sorted(name for name in set(want) & set(got) if want[name] != got[name])
        result = {
            "files": len(got),
            "matched": len(got) - len(mismatched),
            "mismatched": mismatched,
            "missing": missing,
            "extra": extra,
        }
        self.state["readback"] = result
        if mismatched or missing:
            self.log(f"readback mismatch: {len(mismatched)} changed, {len(missing)} missing")
        else:
            self.log(f"readback ok: {len(got)} files match byte for byte")
        # the read-back moved the save out again - put it back untouched
        self.push(staged, with_prefs=False)
        return result

    def unlock(self, options: UnlockOptions | None = None, *, label: str = "unlock") -> dict[str, Any]:
        """Full job: pull -> back up -> patch -> push -> verify."""
        options = options or UnlockOptions()
        report: dict[str, Any] = {"device": self.device.udid, "container": self.device.container}

        self.pull()
        workspace = SaveWorkspace(self.live)
        report["before"] = describe_state(workspace)

        backup = self.backup(self.live, label=f"before-{label}")
        report["backup"] = str(backup)
        report["deviceBackup"] = self.stash_on_device(backup, label=f"before-{label}")

        try:
            report["legacyFilesMoved"] = self.stash_legacy_files(self.live)
            report["changes"] = apply_unlocks(workspace, options, log=self.log)
        except Exception:
            self.log("patch failed - putting the original save back")
            self.push(backup, with_prefs=True)
            raise

        self.push(self.live, with_prefs=True)

        if self.verify:
            readback = self.readback_check(self.live)
            report["readback"] = readback
            if readback["mismatched"] or readback["missing"]:
                raise RuntimeError(
                    "the phone did not keep what was written - the save was restored to the "
                    f"backup in {backup}"
                )

        report["after"] = describe_state(SaveWorkspace(self.live))
        self.state["report"] = report
        return report

    def restore(self, source: Path) -> dict[str, Any]:
        """Put a backup back on the phone."""
        discarded = self.workdir / "restore-discarded"
        try:
            self.pull(into=discarded, with_prefs=True)
            self.log(f"current save kept at {discarded}")
        except Exception as error:  # noqa: BLE001
            self.log(f"container had nothing to move aside ({error})")
        result = self.push(Path(source), with_prefs=True)
        if self.verify:
            result["readback"] = self.readback_check(Path(source))
        return result
