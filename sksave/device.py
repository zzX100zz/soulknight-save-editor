#!/usr/bin/env python3
"""AirLift-based read/write access to an installed iOS app's data container.

airlift (https://github.com/0xjohnnydev/airlift, MIT) only provides a
*fresh-file write* primitive: an attacker-supplied asset is moved through the
Books/AirTraffic code path with an unchecked source path, which lands a brand
new file in an arbitrary directory.  Existing files are never touched and there
is no directory listing.

This driver adds the two missing operations by reusing the same bug:

  pull   moves an EXISTING file/directory out of the container into
         /var/mobile/Media (which AFC can read), copies it to the Mac, and
         leaves the original path empty.
  push   writes a NEW file/directory tree at a path that must currently be
         absent - exactly the state `pull` leaves behind.

pull + (modify locally) + push therefore replaces an existing file, and
pull + push unchanged is a byte-verified round trip.  Every run snapshots and
restores the Books sync state and removes its own Media artifacts.  The
relocated symlink gadget is always removed with a single-path AFC unlink, never
recursively, so cleanup can never follow it into the container.

The target app must not be running while pulling/pushing its files.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import io
import json
import os
import plistlib
import posixpath
import re
import subprocess
import secrets
import shutil
import stat
import struct
import sys
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

PACKAGE = Path(__file__).resolve().parent
PROJECT = PACKAGE.parent
VENDOR = PROJECT / "vendor" / "airlift"
DEVICE_HELPER = VENDOR / "build" / "device_helper"
AIRTRAFFIC_HOST = VENDOR / "build" / "airtraffic_host"
WORKDIR = PROJECT / "work"
JOURNAL = WORKDIR / "journal.jsonl"

# the vendored PoC shells out to `xcrun devicectl`, which needs a real Xcode
_XCODE = Path("/Applications/Xcode.app/Contents/Developer")
if _XCODE.is_dir():
    os.environ.setdefault("DEVELOPER_DIR", str(_XCODE))



MEDIA_ROOT = "/var/mobile/Media"
DEFAULT_BUNDLE = "com.ChillyRoom.DungeonShooter"

# Filled in by the CLI: `configure()` sets these after device/container discovery.
DEFAULT_UDID = os.environ.get("SKSAVE_UDID", "")
SK_CONTAINER = os.environ.get("SKSAVE_CONTAINER", "")


def _const(name: str) -> Any:
    """Constant from the vendored PoC (loads/builds AirLift on first use)."""
    return getattr(vendor(), name)


def configure(*, udid: str | None = None, container: str | None = None) -> None:
    global DEFAULT_UDID, SK_CONTAINER
    if udid:
        DEFAULT_UDID = udid
    if container:
        SK_CONTAINER = container.rstrip("/")


class SkError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def token() -> str:
    return secrets.token_hex(10)  # 20 lowercase hex chars, as the helpers require


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def journal(entry: dict[str, Any]) -> None:
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    entry = {"at": datetime.now(timezone.utc).isoformat(), **entry}
    with JOURNAL.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")


def native(udid: str, command: str, *arguments: str) -> dict[str, Any]:
    result = vendor().native(command, udid, *arguments)
    if result.get("exitCode") != 0 or not result.get("targetGatePassed"):
        raise SkError(f"device_helper {command} failed: {json.dumps(result)[:400]}")
    return result


# --------------------------------------------------------------------------- #
# zip gadget construction
# --------------------------------------------------------------------------- #
def zip_entry(name: str, mode: int) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(2026, 9, 14, 5, 0, 0))
    info.create_system = 3
    info.compress_type = zipfile.ZIP_STORED
    info.external_attr = (mode & 0xFFFF) << 16
    info.extra = struct.pack("<HHH", _const("SZ_EXTRA_ID"), 2, mode & 0xFFFF)
    return info


def build_archive(
    target_tail: str,
    payload: bytes,
    *,
    tree_root: Path | None = None,
    tree_name: str = "tree",
) -> bytes:
    """Reproduce airlift's archive, optionally carrying a whole directory tree.

    Entry order mirrors the vendored PoC (link gadget first, then the target
    directory chain, then the payload) because StreamingZip's symlink
    containment check is sensitive to it.
    """
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", allowZip64=False) as archive:
        archive.writestr(zip_entry("META-INF/", stat.S_IFDIR | 0o755), b"")
        archive.writestr(
            zip_entry("META-INF/com.apple.ZipMetadata.plist", stat.S_IFREG | 0o600),
            plistlib.dumps({"Version": 2}, fmt=plistlib.FMT_BINARY, sort_keys=True),
        )
        for directory in ("p0/", "p0/p1/", "p0/p1/p2/"):
            archive.writestr(zip_entry(directory, stat.S_IFDIR | 0o755), b"")
        archive.writestr(
            zip_entry("p0/p1/p2/link", stat.S_IFLNK | 0o777),
            f"../../../{target_tail}".encode(),
        )
        cursor = ""
        for component in target_tail.split("/"):
            cursor += component + "/"
            archive.writestr(zip_entry(cursor, stat.S_IFDIR | 0o755), b"")
        archive.writestr(zip_entry("payload", stat.S_IFREG | 0o600), payload)

        if tree_root is not None:
            prefix = f"{tree_name}/"
            archive.writestr(zip_entry(prefix, stat.S_IFDIR | 0o755), b"")
            for path in sorted(tree_root.rglob("*")):
                relative = path.relative_to(tree_root).as_posix()
                name = prefix + relative
                if path.is_symlink():
                    archive.writestr(
                        zip_entry(name, stat.S_IFLNK | 0o777),
                        os.readlink(path).encode(),
                    )
                elif path.is_dir():
                    archive.writestr(zip_entry(name + "/", stat.S_IFDIR | 0o755), b"")
                else:
                    archive.writestr(
                        zip_entry(name, stat.S_IFREG | (path.stat().st_mode & 0o777)),
                        path.read_bytes(),
                    )
    return output.getvalue()


# --------------------------------------------------------------------------- #
# AFC access to /var/mobile/Media (via pymobiledevice3)
# --------------------------------------------------------------------------- #
def afc(coro_factory, udid: str = DEFAULT_UDID) -> Any:
    async def runner():
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.services.afc import AfcService

        lockdown = await create_using_usbmux(serial=udid)
        service = AfcService(lockdown)
        async with service:
            return await coro_factory(service)

    return asyncio.run(runner())


def media_stat(path: str, udid: str = DEFAULT_UDID) -> dict[str, Any]:
    async def job(service):
        try:
            return await service.stat(path)
        except Exception:  # noqa: BLE001
            return {}

    return afc(job, udid)


def media_rm_single(path: str, udid: str = DEFAULT_UDID) -> bool:
    """Single REMOVE_PATH - never recurses, so it cannot chase a symlink."""

    async def job(service):
        try:
            return await service.rm_single(path, force=True)
        except Exception:  # noqa: BLE001
            return False

    return afc(job, udid)


def media_rm_tree(path: str, udid: str = DEFAULT_UDID) -> bool:
    """Recursive removal, used only for directories this tool created."""

    async def job(service):
        try:
            await service.rm(path, force=True)
            return True
        except Exception:  # noqa: BLE001
            return False

    return afc(job, udid)


def media_remove(path: str, udid: str = DEFAULT_UDID) -> bool:
    """Remove a Media path, recursing only when it is a real directory."""
    info = media_stat(path, udid)
    if not info:
        return True
    if info.get("st_ifmt") == "S_IFDIR":
        return media_rm_tree(path, udid)
    return media_rm_single(path, udid)


def media_identifier(path: str) -> str:
    """Books identifier for an AFC-style Media path (which is Media-relative).

    AFC paths like ``/airlift-recovered-x`` mean ``/var/mobile/Media/...``, so
    they must be anchored to Media before being made relative to the Airlock
    dir - otherwise the device resolves the source to a nonexistent path and
    silently skips the move.
    """
    absolute = path if path.startswith(MEDIA_ROOT) else MEDIA_ROOT + "/" + path.lstrip("/")
    return posixpath.relpath(posixpath.normpath(absolute), _const("AIRLOCK_ROOT"))


def media_list(path: str, depth: int = 1, udid: str = DEFAULT_UDID) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    async def job(service):
        async def walk(current: str, level: int):
            try:
                names = await service.listdir(current)
            except Exception as error:  # noqa: BLE001
                rows.append(
                    {"path": current, "error": f"{type(error).__name__}: {error}"[:160]}
                )
                return
            for name in sorted(names):
                if name in (".", ".."):
                    continue
                full = posixpath.join(current, name)
                try:
                    info = await service.stat(full)
                except Exception:  # noqa: BLE001
                    info = {}
                kind = {
                    "S_IFDIR": "dir",
                    "S_IFLNK": "symlink",
                    "S_IFREG": "file",
                }.get(info.get("st_ifmt"), "?")
                row: dict[str, Any] = {"path": full, "kind": kind}
                if info.get("st_size") is not None:
                    row["size"] = int(info["st_size"])
                if info.get("LinkTarget"):
                    row["linkTarget"] = info["LinkTarget"]
                rows.append(row)
                if kind == "dir" and level < depth:
                    await walk(full, level + 1)

        await walk(path, 1)

    afc(job, udid)
    return rows


def media_read(path: str, udid: str = DEFAULT_UDID) -> bytes | None:
    async def job(service):
        try:
            return await service.get_file_contents(path)
        except Exception:  # noqa: BLE001
            return None

    return afc(job, udid)


def media_mkdir(path: str, udid: str = DEFAULT_UDID) -> bool:
    """Create a directory (and parents) inside /var/mobile/Media."""

    async def job(service):
        try:
            await service.makedirs(path)
            return True
        except Exception:  # noqa: BLE001
            return False

    return afc(job, udid)


def media_write(path: str, data: bytes, udid: str = DEFAULT_UDID) -> dict[str, Any]:
    """Create or overwrite a file inside /var/mobile/Media."""

    async def job(service):
        try:
            await service.set_file_contents(path, data)
        except Exception as error:  # noqa: BLE001
            return {"ok": False, "error": f"{type(error).__name__}: {error}"[:200]}
        return {"ok": True}

    result = afc(job, udid)
    if not result.get("ok"):
        raise SkError(f"media write failed: {result.get('error')}")
    back = media_read(path, udid)
    return {
        "ok": back == data,
        "mediaPath": path,
        "bytes": len(data),
        "sha256": sha256(data),
        "readBackSha256": sha256(back) if back is not None else None,
    }


def media_push_tree(
    local_root: Path, remote_root: str, udid: str = DEFAULT_UDID
) -> dict[str, Any]:
    """Upload a local directory tree into /var/mobile/Media via AFC."""

    async def job(service):
        await service.push(str(local_root), remote_root, progress_bar=False)
        return True

    afc(job, udid)
    return {"ok": True, "mediaPath": remote_root}


def media_pull_tree(
    remote_root: str, local_root: Path, udid: str = DEFAULT_UDID
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []

    async def job(service):
        try:
            info = await service.stat(remote_root)
        except Exception:  # noqa: BLE001
            return
        if info.get("st_ifmt") != "S_IFDIR":
            data = await service.get_file_contents(remote_root)
            local_root.parent.mkdir(parents=True, exist_ok=True)
            local_root.write_bytes(data)
            entries.append(
                {
                    "path": local_root.name,
                    "kind": "file",
                    "bytes": len(data),
                    "sha256": sha256(data),
                }
            )
            return
        local_root.mkdir(parents=True, exist_ok=True)
        async for dirpath, dirnames, filenames in service.walk(remote_root):
            relative_dir = os.path.relpath(dirpath, remote_root)
            relative_dir = "" if relative_dir == "." else relative_dir
            target_dir = local_root / relative_dir
            target_dir.mkdir(parents=True, exist_ok=True)
            for name in dirnames:
                (target_dir / name).mkdir(exist_ok=True)
                entries.append(
                    {"path": posixpath.join(relative_dir, name), "kind": "dir"}
                )
            for name in filenames:
                source = posixpath.join(dirpath, name)
                try:
                    data = await service.get_file_contents(source)
                except Exception as error:  # noqa: BLE001
                    entries.append(
                        {
                            "path": posixpath.join(relative_dir, name),
                            "kind": "error",
                            "error": f"{type(error).__name__}: {error}"[:200],
                        }
                    )
                    continue
                (target_dir / name).write_bytes(data)
                entries.append(
                    {
                        "path": posixpath.join(relative_dir, name),
                        "kind": "file",
                        "bytes": len(data),
                        "sha256": sha256(data),
                    }
                )

    afc(job, udid)
    return entries


# --------------------------------------------------------------------------- #
# one airlift cycle: snapshot -> stage -> (caller syncs) -> cleanup
# --------------------------------------------------------------------------- #
@contextlib.contextmanager
def airlift_cycle(
    udid: str,
    *,
    target_dir: str,
    payload: bytes,
    identifiers: list[str],
    tree_root: Path | None = None,
) -> Iterator[dict[str, Any]]:
    # the helper requires all three generated names to share one token
    ident = token()
    source = f"{_const('SOURCE_PREFIX')}{ident}"
    link = f"{_const('LINK_PREFIX')}{ident}"
    recovered = f"{_const('RECOVERED_PREFIX')}{ident}"
    snapshot_root = Path(tempfile.mkdtemp(prefix="skairlift-"))
    (snapshot_root / "payload.zip").write_bytes(
        build_archive(target_dir[1:], payload, tree_root=tree_root)
    )
    # identifiers are resolved from the Airlock dir, so they need the ../../ hop
    resolved = [item.replace("@SRC@", f"../../{source}") for item in identifiers]
    (snapshot_root / "Books.plist").write_bytes(vendor().build_books(resolved))

    info: dict[str, Any] = {
        "source": source,
        "link": link,
        "recovered": recovered,
        "identifiers": resolved,
    }
    try:
        native(udid, "snapshot-books", os.fspath(snapshot_root))
        stage = native(
            udid,
            "stage",
            source,
            link,
            recovered,
            os.fspath(snapshot_root / "payload.zip"),
            os.fspath(snapshot_root / "Books.plist"),
            os.fspath(snapshot_root),
        )
        info["stage"] = stage.get("operation", {})
        if not info["stage"].get("ok"):
            raise SkError(f"stage refused: {json.dumps(info['stage'])[:300]}")
        yield info
    finally:
        # leftover extraction tree (a directory we made) - recursive is fine
        if media_stat("/" + source, udid):
            media_rm_tree("/" + source, udid)
        # the relocated symlink gadget must be unlinked, never traversed
        if media_stat("/" + link, udid):
            media_rm_single("/" + link, udid)
        try:
            native(udid, "restore-books", os.fspath(snapshot_root))
        except Exception as error:  # noqa: BLE001
            info["restoreError"] = str(error)[:200]
        shutil.rmtree(snapshot_root, ignore_errors=True)


def sync(udid: str, pairs: list[tuple[str, str]]) -> dict[str, Any]:
    command = [os.fspath(AIRTRAFFIC_HOST), udid]
    for identifier, destination in pairs:
        command.extend((identifier, destination))
    result = vendor().run_json(command, timeout=180)
    if result.get("exitCode") != 0:
        raise SkError(f"AirTraffic sync failed: {json.dumps(result)[:300]}")
    time.sleep(2)
    return result


# --------------------------------------------------------------------------- #
# operations
# --------------------------------------------------------------------------- #
def pull(udid: str, remote_path: str, local_path: Path) -> dict[str, Any]:
    """Move ``remote_path`` out of the container into Media, then copy it here."""
    remote_path = posixpath.normpath(remote_path)
    identifier = posixpath.relpath(remote_path, _const("AIRLOCK_ROOT"))
    # airtraffic_host insists on at least two asset pairs, so the (harmless)
    # symlink relocation rides along with the real read
    with airlift_cycle(
        udid,
        target_dir=posixpath.dirname(remote_path),
        payload=b"airlift pull placeholder",
        identifiers=["@SRC@/p0/p1/p2/link", identifier],
    ) as info:
        info["airTraffic"] = sync(
            udid,
            [
                (f"../../{info['source']}/p0/p1/p2/link", info["link"]),
                (identifier, info["recovered"]),
            ],
        )

    media_path = "/" + info["recovered"]
    stat_info = media_stat(media_path, udid)
    if not stat_info:
        raise SkError(
            f"pull failed: {remote_path} did not reach Media "
            "(wrong path, or the app recreated/held it)"
        )
    entries = media_pull_tree(media_path, Path(local_path), udid)
    return {
        "ok": True,
        "remotePath": remote_path,
        "localPath": str(local_path),
        "mediaPath": media_path,
        "kind": "dir" if stat_info.get("st_ifmt") == "S_IFDIR" else "file",
        "bytes": int(stat_info.get("st_size", 0)),
        "entries": entries,
    }


def push(udid: str, local_path: Path, remote_path: str) -> dict[str, Any]:
    """Write a NEW file at ``remote_path`` (the path must not exist)."""
    remote_path = posixpath.normpath(remote_path)
    leaf = posixpath.basename(remote_path)
    payload = local_path.read_bytes()
    with airlift_cycle(
        udid,
        target_dir=posixpath.dirname(remote_path),
        payload=payload,
        identifiers=["@SRC@/p0/p1/p2/link", "@SRC@/payload"],
    ) as info:
        info["airTraffic"] = sync(
            udid,
            [
                (f"../../{info['source']}/p0/p1/p2/link", info["link"]),
                (f"../../{info['source']}/payload", f"{info['link']}/{leaf}"),
            ],
        )
    return {
        "ok": True,
        "remotePath": remote_path,
        "bytes": len(payload),
        "sha256": sha256(payload),
    }


def push_tree(udid: str, local_root: Path, remote_path: str) -> dict[str, Any]:
    """Write a NEW directory tree at ``remote_path`` (must not exist)."""
    remote_path = posixpath.normpath(remote_path)
    leaf = posixpath.basename(remote_path)
    with airlift_cycle(
        udid,
        target_dir=posixpath.dirname(remote_path),
        payload=b"airlift tree placeholder",
        identifiers=["@SRC@/p0/p1/p2/link", "@SRC@/tree"],
        tree_root=local_root,
    ) as info:
        info["airTraffic"] = sync(
            udid,
            [
                (f"../../{info['source']}/p0/p1/p2/link", info["link"]),
                (f"../../{info['source']}/tree", f"{info['link']}/{leaf}"),
            ],
        )
    return {
        "ok": True,
        "remotePath": remote_path,
        "entries": len(list(local_root.rglob("*"))),
    }


def move_into_container(
    udid: str, media_path: str, remote_path: str
) -> dict[str, Any]:
    """Move an EXISTING item from Media into the app container (reverse of pull).

    The device standardises the destination *before* the prefix check, so a
    ``..`` hop is rejected outright (verified: the move silently does nothing).
    The documented escape is an ancestor symlink - the string check sees a path
    under Media, then the filesystem follows the symlink into the container.
    The zip gadget already relocates exactly such a symlink (pointing at the
    target directory) into Media, so reuse it as the destination's ancestor.
    """
    media_path = posixpath.normpath(media_path)
    remote_path = posixpath.normpath(remote_path)
    leaf = posixpath.basename(remote_path)
    identifier = media_identifier(media_path)
    with airlift_cycle(
        udid,
        target_dir=posixpath.dirname(remote_path),
        payload=b"airlift move placeholder",
        identifiers=["@SRC@/p0/p1/p2/link", identifier],
    ) as info:
        info["airTraffic"] = sync(
            udid,
            [
                (f"../../{info['source']}/p0/p1/p2/link", info["link"]),
                (identifier, f"/{info['link']}/{leaf}"),
            ],
        )
    moved = media_stat(media_path, udid) == {}
    return {
        "ok": moved,
        "mediaPath": media_path,
        "remotePath": remote_path,
        "viaLink": f"/{info['link']}/{leaf}",
        "moved": moved,
    }


def self_test(udid: str, target_dir: str, size: int = 262_144) -> dict[str, Any]:
    """push -> pull -> compare, twice, using our own file in the container."""
    leaf = f"airlift-selftest-{secrets.token_hex(8)}.bin"
    remote = posixpath.join(target_dir, leaf)
    work = Path(tempfile.mkdtemp(prefix="skairlift-test-"))
    report: dict[str, Any] = {"remote": remote, "size": size, "steps": []}
    try:
        first = secrets.token_bytes(size)
        source = work / "first.bin"
        source.write_bytes(first)
        report["steps"].append({"push1": push(udid, source, remote)})

        back = work / "back1.bin"
        pulled = pull(udid, remote, back)
        report["steps"].append(
            {"pull1": {k: v for k, v in pulled.items() if k != "entries"}}
        )
        report["firstMatch"] = sha256(back.read_bytes()) == sha256(first)

        second = secrets.token_bytes(size // 2)
        source2 = work / "second.bin"
        source2.write_bytes(second)
        report["steps"].append({"push2": push(udid, source2, remote)})
        back2 = work / "back2.bin"
        pulled2 = pull(udid, remote, back2)
        report["steps"].append(
            {"pull2": {k: v for k, v in pulled2.items() if k != "entries"}}
        )
        report["secondMatch"] = sha256(back2.read_bytes()) == sha256(second)

        # the final pull left the file in Media; drop that copy too
        report["mediaRemoved"] = media_remove(pulled2["mediaPath"], udid)
        report["ok"] = bool(report["firstMatch"] and report["secondMatch"])
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return report


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# vendored AirLift: import lazily, clone + build on demand
# --------------------------------------------------------------------------- #
AIRLIFT_REPO = "https://github.com/0xjohnnydev/airlift"
_vendored: Any = None


def ensure_airlift(*, build: bool = True, quiet: bool = False) -> Path:
    """Make sure the AirLift PoC is present locally and built."""
    def log(message: str) -> None:
        if not quiet:
            print(message, flush=True)

    if not (VENDOR / "airlift.py").is_file():
        VENDOR.parent.mkdir(parents=True, exist_ok=True)
        log(f"cloning {AIRLIFT_REPO} -> {VENDOR}")
        subprocess.run(["git", "clone", "--depth", "1", AIRLIFT_REPO, os.fspath(VENDOR)], check=True)
    if build and not DEVICE_HELPER.is_file():
        log("building AirLift (make) ...")
        subprocess.run(["make", "-C", os.fspath(VENDOR)], check=True)
    if build and not DEVICE_HELPER.is_file():
        raise SkError(f"AirLift did not build: {DEVICE_HELPER} is missing")
    return VENDOR


def vendor() -> Any:
    """Import the vendored AirLift module (cloning/building it on first use)."""
    global _vendored
    if _vendored is None:
        ensure_airlift()
        if os.fspath(VENDOR) not in sys.path:
            sys.path.insert(0, os.fspath(VENDOR))
        import airlift as module  # noqa: PLC0415  (vendored PoC primitives)

        _vendored = module
    return _vendored


def _devicectl_json(arguments: list[str], timeout: int = 30) -> dict[str, Any]:
    """Run devicectl and parse its JSON output.

    That output is pretty-printed, so it cannot be parsed line by line the way the
    vendored PoC helper does; the first JSON object in the stream is used instead.
    """
    completed = subprocess.run(
        ["xcrun", "devicectl", *arguments, "--timeout", "8", "--json-output", "-"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=timeout,
    )
    text = completed.stdout or ""
    start = text.find("{")
    if start < 0:
        raise SkError(f"devicectl returned no JSON (exit {completed.returncode})")
    payload, _ = json.JSONDecoder().raw_decode(text[start:])
    return payload


def list_devices() -> list[dict[str, Any]]:
    """Paired physical iPhones that AirLift can talk to."""
    payload = _devicectl_json(["list", "devices"])
    devices = (payload.get("result") or {}).get("devices") or []
    return vendor().available_devices(devices)


def resolve(udid: str | None = None) -> dict[str, Any]:
    """Pick a device (the only one, or the requested UDID) without prompting."""
    devices = list_devices()
    if not devices:
        raise SkError("no paired physical iPhone found - pair the phone and enable Developer Mode")
    if udid:
        for device in devices:
            if device["udid"].casefold() == udid.casefold():
                return device
        raise SkError(f"{udid} is not a paired, available iPhone")
    if len(devices) > 1:
        raise SkError("several iPhones are paired - pass --udid (see `devices`)")
    return devices[0]


_CONTAINER_RE = re.compile(r"/var/mobile/Containers/Data/Application/[0-9A-Fa-f-]{36}")


def find_container(udid: str, bundle_id: str, *, explicit: str | None = None,
                   cache: Path | None = None) -> str:
    """Locate the app's data container through CoreDevice.

    CoreDevice refuses to answer while the iPhone is locked, so the last path that
    worked is kept in ``cache`` and reused as a fallback: the container only moves
    when the app is reinstalled or updated.
    """
    if explicit:
        return explicit.rstrip("/")
    if not udid:
        udid = resolve(None)
    commands = (
        ["xcrun", "devicectl", "device", "info", "files", "--device", udid,
         "--domain-type", "appDataContainer", "--domain-identifier", bundle_id, "--json-output", "-"],
        ["xcrun", "devicectl", "device", "info", "apps", "--device", udid,
         "--bundle-id", bundle_id, "--json-output", "-"],
    )
    reasons: list[str] = []
    for command in commands:
        try:
            completed = subprocess.run(command, check=False, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True, timeout=30)
        except subprocess.SubprocessError as error:
            reasons.append(f"{command[4]}: {error}")
            continue
        output = completed.stdout or ""
        match = _CONTAINER_RE.search(output)
        if match:
            if cache is not None:
                try:
                    cache.parent.mkdir(parents=True, exist_ok=True)
                    cache.write_text(match.group(0) + "\n")
                except OSError:
                    pass
            return match.group(0)
        # keep the device's own words: "failed to get a list of files on the remote
        # device" is what a locked iPhone answers, and it is the only honest clue.
        note = _device_reason(output)
        if note:
            reasons.append(f"{command[4]}: {note}")
    if cache is not None and cache.is_file():
        remembered = cache.read_text().strip()
        if _CONTAINER_RE.fullmatch(remembered):
            return remembered
    detail = ("\n  device said: " + " | ".join(reasons)) if reasons else ""
    raise SkError(
        "could not read the app data container.\n"
        "  Unlock the iPhone right before pressing the button - iOS refuses app data while "
        "the screen is locked, even over USB, and the phone re-locks quickly.  Setting "
        "Settings > Display & Brightness > Auto-Lock to Never while you use this tool helps.\n"
        "  Use a USB cable rather than Wi-Fi: over the network the same request is refused.\n"
        "  Last resort: pass the path yourself, "
        "--container /var/mobile/Containers/Data/Application/<UUID>"
        + detail
    )


def _device_reason(output: str) -> str:
    """Pull the most useful sentence out of devicectl's output.

    "The system failed to get a list of files on the remote device" says far more
    than the generic "CoreDevice.ActionError error 3" printed above it.
    """
    best = ""
    for line in (output or "").splitlines():
        line = line.strip().split("=", 1)[-1].strip().strip('",')
        if len(line) <= 12:
            continue
        lowered = line.lower()
        if "failed to" in lowered or "not unlocked" in lowered or "locked" in lowered:
            return line[:220]
        if not best and ("nsdebugdescription" in lowered or "error" in lowered):
            best = line[:220]
    return best
