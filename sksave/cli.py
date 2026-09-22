"""Command line interface.

    python3 run.py                 # interactive menu
    python3 run.py setup           # clone + build AirLift
    python3 run.py devices         # list paired iPhones
    python3 run.py info            # read the save, change nothing
    python3 run.py backup          # pull a copy, keep it on the Mac and on the phone
    python3 run.py unlock          # apply the unlocks
    python3 run.py restore --list  # list the backups stored inside the phone
    python3 run.py restore --from backups/20260922-101500

Every device command must run while the game is closed; AirLift moves files out
of the container, so the app must not hold them open.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from . import device as dev
from . import fields
from .patcher import UnlockOptions, apply as apply_unlocks, describe_state
from .session import SaveSession, connect
from .workspace import SaveWorkspace

PROJECT = Path(__file__).resolve().parent.parent


def log(message: str) -> None:
    print(f"  {message}", flush=True)


def _print_json(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False))


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def command_setup(args) -> int:
    print("checking the local toolchain")
    for tool in ("git", "make", "xcrun"):
        print(f"  {tool}: {shutil.which(tool) or 'MISSING'}")
    vendor = dev.ensure_airlift(build=not args.no_build)
    print(f"  AirLift: {vendor}")
    print(f"  device_helper: {'ok' if dev.DEVICE_HELPER.is_file() else 'MISSING'}")
    if args.no_build:
        return 0
    try:
        devices = dev.list_devices()
    except Exception as error:  # noqa: BLE001
        print(f"  cannot list devices yet: {error}")
        return 1
    if not devices:
        print("  no paired iPhone found")
        return 1
    for entry in devices:
        print(f"  {entry['name']}  {entry['product']}  iOS {entry['version']}  {entry['udid']}")
    return 0


def command_devices(args) -> int:
    for entry in dev.list_devices():
        tested = "tested" if entry.get("tested") else "untested build"
        print(f"{entry['name']}\t{entry['product']}\tiOS {entry['version']}\t{entry['transport']}\t{tested}\t{entry['udid']}")
    return 0


def _open_session(args) -> SaveSession:
    workdir = Path(args.workdir or (PROJECT / "work")).expanduser()
    workdir.mkdir(parents=True, exist_ok=True)
    device = connect(args.udid, args.container, log=log)
    return SaveSession(device, workdir, log=log, verify=not args.no_verify)


def command_info(args) -> int:
    if args.offline:
        _print_json(SaveWorkspace(Path(args.offline).expanduser()).describe())
        return 0
    session = _open_session(args)
    session.pull()
    state = SaveWorkspace(session.live).describe()
    session.push(session.live)                     # put the untouched save back
    _print_json(state)
    return 0


def command_backup(args) -> int:
    session = _open_session(args)
    session.pull()
    path = session.backup(session.live, label=args.label or "manual")
    session.stash_on_device(path, label=args.label or "manual")
    session.push(session.live)
    print(f"backup written to {path}")
    return 0


def _options_from_args(args) -> UnlockOptions:
    options = UnlockOptions()
    for name in (
        "heroes", "hero_levels", "skins", "pets", "skills", "weapons", "weapon_skins",
        "evolution", "kill_effects", "mythic", "materials", "season_coin", "gems",
        "repair_format",
    ):
        flag = getattr(args, f"no_{name}", False)
        if flag:
            setattr(options, name, False)
    if args.gems is not None:
        options.gems_value = args.gems
    if args.season_coin is not None:
        options.season_coin_value = args.season_coin
    if args.quantity is not None:
        options.quantity = args.quantity
    if args.hero_level is not None:
        options.hero_level = args.hero_level
    return options


def command_unlock(args) -> int:
    options = _options_from_args(args)
    if args.offline:
        root = Path(args.offline).expanduser()
        workspace = SaveWorkspace(root)
        print(f"patching {workspace.documents} in place (offline mode)")
        legacy = workspace.legacy_files()
        if legacy and args.strip_legacy:
            aside = root / "legacy-new"
            aside.mkdir(parents=True, exist_ok=True)
            for path in legacy:
                shutil.move(str(path), str(aside / path.name))
            print(f"  moved {len(legacy)} new-format files to {aside}")
        elif legacy:
            print(f"  note: {len(legacy)} *.data.new/*.data.rij files are present - the game reads "
                  f"those instead of the edited shards (use --strip-legacy to move them aside)")
        report = apply_unlocks(workspace, options, log=log)
        _print_json({"changes": report, "after": describe_state(workspace)})
        return 0

    session = _open_session(args)
    report = session.unlock(options, label=args.label or "unlock")
    print_summary(report)

    print(f"\nbackup: {report['backup']}")
    if report.get("deviceBackup"):
        print(f"on the phone: {report['deviceBackup']}")
    print("\nDone. Keep the game closed until you finish reading this, then start it once.")
    return 0


def print_summary(report: dict) -> None:
    """Human-readable before/after table for `unlock`."""
    before, after = report["before"], report["after"]
    print("\nbefore -> after")
    for key, label in (("heroes", "heroes locked"), ("pets", "pets locked"),
                       ("skins", "skins locked"), ("skills", "skills locked")):
        print(f"  {label:20s} {before[key]['locked']:>6}  ->  {after[key]['locked']}")
    print(f"  {'weapons unlocked':20s} {before['weapons']['unlocked']:>6}  ->  "
          f"{after['weapons']['unlocked']} / {after['weapons']['catalog']}")
    print(f"  {'weapon evolution':20s} {before['weapons']['evolved']:>6}  ->  {after['weapons']['evolved']}")
    print(f"  {'kill effects':20s} {before['killEffects']:>6}  ->  {after['killEffects']}")
    print(f"  {'gems':20s} {before['gems']:>6}  ->  {after['gems']}")
    print(f"  {'season coins':20s} {before['seasonCoin']:>6}  ->  {after['seasonCoin']}")
    print(f"  {'format switches':20s} {json.dumps(before['legacyFlags'])}  ->  {json.dumps(after['legacyFlags'])}")


def command_restore(args) -> int:
    session = _open_session(args)
    if args.list:
        for remote in session.device_backups():
            print(remote)
        backups = sorted(path for path in session.backups.iterdir() if path.is_dir()) if session.backups.is_dir() else []
        for local in backups:
            print(f"{local}  (local)")
        return 0
    if args.from_device:
        session.restore_from_device(args.from_device)
        print(f"restored from {args.from_device}")
        return 0
    if not args.from_path:
        print("pass --from <backup dir> or --from-device <media path> (see --list)", file=sys.stderr)
        return 2
    session.restore(Path(args.from_path).expanduser())
    print(f"restored from {args.from_path}")
    return 0


# --------------------------------------------------------------------------- #
# interactive menu
# --------------------------------------------------------------------------- #
MENU = """
Soul Knight iOS save editor (AirLift)

  1  check the setup (AirLift, Xcode, paired iPhone)
  2  list paired iPhones
  3  read the save (changes nothing)
  4  back up the save (Mac + phone)
  5  unlock everything
  6  restore a backup
  q  quit
"""


def command_menu(args) -> int:
    print(MENU)
    while True:
        try:
            choice = input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if choice in {"q", "quit", "exit"}:
            return 0
        namespace = argparse.Namespace(**vars(args))
        namespace.offline = getattr(args, "offline", None)
        namespace.from_path = getattr(args, "from_path", None)
        namespace.from_device = getattr(args, "from_device", None)
        namespace.list = False
        namespace.label = None
        if choice == "1":
            command_setup(namespace)
        elif choice == "2":
            command_devices(namespace)
        elif choice == "3":
            command_info(namespace)
        elif choice == "4":
            command_backup(namespace)
        elif choice == "5":
            command_unlock(namespace)
        elif choice == "6":
            command_restore(argparse.Namespace(**{**vars(namespace), "list": True}))
            source = input("backup path (empty to cancel): ").strip()
            if source:
                command_restore(argparse.Namespace(**{**vars(namespace), "from_path": source}))
        else:
            print(MENU)


# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sksave",
        description="Soul Knight iOS save editor, driven over AirLift.",
    )
    parser.add_argument("--udid", help="iPhone UDID (needed only when several are paired)")
    parser.add_argument("--container", help="app data container path (skips discovery)")
    parser.add_argument("--workdir", help="where to keep pulled files and backups (default ./work)")
    parser.add_argument("--no-verify", action="store_true", help="skip the read-back comparison")
    parser.add_argument("--offline", help="operate on a local save copy instead of a phone")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("setup", help="clone and build AirLift, then check the toolchain")
    p.add_argument("--no-build", action="store_true")
    p.set_defaults(func=command_setup)

    sub.add_parser("devices", help="list paired iPhones").set_defaults(func=command_devices)

    p = sub.add_parser("info", help="read the save and print what is locked")
    p.set_defaults(func=command_info)

    p = sub.add_parser("backup", help="pull a copy of the save")
    p.add_argument("--label", help="name for this backup")
    p.set_defaults(func=command_backup)

    p = sub.add_parser("unlock", help="unlock heroes, skins, weapons, effects and currencies")
    for name in (
        "heroes", "hero_levels", "skins", "pets", "skills", "weapons", "weapon_skins",
        "evolution", "kill_effects", "mythic", "materials", "season_coin", "gems",
        "repair_format",
    ):
        p.add_argument(f"--no-{name.replace('_', '-')}", dest=f"no_{name}", action="store_true",
                       help=f"leave {name.replace('_', ' ')} alone")
    p.add_argument("--gems", type=int, help=f"gem target (default {fields.GEM_MAX})")
    p.add_argument("--season-coin", type=int, help=f"season coin target (default {fields.SEASON_COIN_MAX})")
    p.add_argument("--quantity", type=int, help=f"materials/seeds/tickets target (default {fields.MAX_QUANTITY})")
    p.add_argument("--hero-level", type=int, help=f"hero level target (default {fields.HERO_LEVEL_MAX})")
    p.add_argument("--label", help="name for the pre-change backup")
    p.add_argument("--strip-legacy", action="store_true",
                   help="offline mode: move *.data.new/*.data.rij aside so the game uses the shards")
    p.set_defaults(func=command_unlock)

    p = sub.add_parser("restore", help="put a backup back on the phone")
    p.add_argument("--from", dest="from_path", help="local backup directory")
    p.add_argument("--from-device", help="backup directory inside the phone's Media folder")
    p.add_argument("--list", action="store_true", help="list available backups")
    p.set_defaults(func=command_restore)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        args.func = command_menu
    for attribute, default in (("offline", None), ("no_verify", False), ("udid", None),
                               ("container", None), ("workdir", None)):
        if not hasattr(args, attribute):
            setattr(args, attribute, default)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except Exception as error:  # noqa: BLE001
        print(f"error: {error}", file=sys.stderr)
        return 1
