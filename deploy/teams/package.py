"""Build and check the OmniFile Teams app package (deploy/teams/build.sh runs this).

    package.py build --bot-app-id GUID --portal-url URL --schema FILE --out ZIP
                     [--version X.Y.Z] [--app-id GUID] [--name NAME] [--relay-host HOST]
    package.py validate --schema FILE MANIFEST_OR_ZIP

The package is the three files Teams wants at the root of a zip: manifest.json
(manifest schema 1.17), color.png (192x192, full colour) and outline.png (32x32,
white on transparent). The icons are drawn here, so the repository holds no
binary. The zip is deterministic (fixed times and order), so the same inputs give
the same bytes.

The app is the bot alone: OmniFile's recall cards in a personal chat
(teams.md §6). Users don't talk to it; the cards' buttons are Action.Execute
invokes, which Teams sends to the bot's endpoint (the relay).
"""

from __future__ import annotations

import argparse
import io
import json
import re
import struct
import sys
import uuid
import zipfile
import zlib
from pathlib import Path

import jsonschema

MANIFEST_VERSION = "1.17"
SCHEMA_URL = (
    f"https://developer.microsoft.com/json-schemas/teams/v{MANIFEST_VERSION}"
    "/MicrosoftTeams.schema.json"
)
ACCENT = (0x1F, 0x4E, 0x79)  # the colour behind the colour icon
GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
#: The manifest id for a bot id, when none is given: the same every build, so an upload
#: of a new version updates the app rather than adding another one.
APP_ID_NAMESPACE = uuid.UUID("6f0b6d1e-8a64-4f8e-9b7d-6b1c2f3a4d5e")
ZIP_TIME = (2026, 1, 1, 0, 0, 0)


# --- icons ---------------------------------------------------------------------------


def _png(width: int, height: int, pixel) -> bytes:
    """An RGBA PNG; `pixel(x, y)` gives (r, g, b, a)."""
    rows = b"".join(
        b"\x00" + b"".join(bytes(pixel(x, y)) for x in range(width)) for y in range(height)
    )

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows, 9))
        + chunk(b"IEND", b"")
    )


def _ring(size: int, x: int, y: int) -> bool:
    """A ring with a gap (an 'O' for OmniFile) on a size x size grid."""
    c = (size - 1) / 2
    d = ((x - c) ** 2 + (y - c) ** 2) ** 0.5
    return 0.22 * size <= d <= 0.36 * size and not (x > c and abs(y - c) < 0.06 * size)


def color_icon() -> bytes:
    return _png(192, 192, lambda x, y: (255, 255, 255, 255) if _ring(192, x, y) else (*ACCENT, 255))


def outline_icon() -> bytes:
    # White on transparent, nothing else (Teams' rule for the outline icon).
    return _png(32, 32, lambda x, y: (255, 255, 255, 255) if _ring(32, x, y) else (0, 0, 0, 0))


def png_size(data: bytes) -> tuple[int, int]:
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        raise ValueError("not a PNG")
    return struct.unpack(">II", data[16:24])


# --- manifest ------------------------------------------------------------------------


def manifest(
    *,
    bot_app_id: str,
    portal_url: str,
    version: str,
    app_id: str | None = None,
    name: str = "OmniFile",
    relay_host: str | None = None,
) -> dict:
    bot = bot_app_id.lower()
    if not GUID.match(bot):
        raise ValueError(f"--bot-app-id {bot_app_id!r} is not a GUID (the bot's Microsoft App ID)")
    if not re.match(r"^https://[^/\s]+", portal_url):
        raise ValueError("--portal-url must be https://")
    app = (app_id or str(uuid.uuid5(APP_ID_NAMESPACE, bot))).lower()
    if not GUID.match(app):
        raise ValueError(f"--app-id {app_id!r} is not a GUID")
    portal = portal_url.rstrip("/")
    out = {
        "$schema": SCHEMA_URL,
        "manifestVersion": MANIFEST_VERSION,
        "version": version,
        "id": app,
        "developer": {
            "name": "OmniFile Gateway",
            "websiteUrl": portal,
            "privacyUrl": portal,
            "termsOfUseUrl": portal,
        },
        "name": {"short": name[:30], "full": f"{name} archive and recall"[:100]},
        "description": {
            "short": "Recall archived files and follow their progress."[:80],
            "full": (
                "OmniFile tells you when a file you opened is in archive storage, and lets"
                " you recall it, keep it online or track the recall from the card. It"
                " never shows a file your AD groups can't read."
            ),
        },
        "icons": {"color": "color.png", "outline": "outline.png"},
        "accentColor": "#{:02X}{:02X}{:02X}".format(*ACCENT),
        "bots": [
            {
                "botId": bot,
                "scopes": ["personal"],
                "supportsFiles": False,
                # The bot only sends cards, but their buttons are invokes the bot must receive.
                # VERIFY on a real tenant whether isNotificationOnly would still deliver them.
                "isNotificationOnly": False,
            }
        ],
    }
    if relay_host:
        out["validDomains"] = [relay_host]
    return out


def validate(doc: dict, schema: dict) -> list[str]:
    """Every way `doc` breaks the manifest schema, plus the rules the schema can't say."""
    validator = jsonschema.Draft4Validator(schema)
    errors = [
        f"{'/'.join(map(str, e.absolute_path)) or '(root)'}: {e.message}"
        for e in sorted(validator.iter_errors(doc), key=lambda e: list(e.absolute_path))
    ]
    if doc.get("manifestVersion") != MANIFEST_VERSION:
        errors.append(f"manifestVersion is not {MANIFEST_VERSION}")
    return errors


def check_package(zip_bytes: bytes, schema: dict) -> list[str]:
    errors = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        names = sorted(z.namelist())
        if names != ["color.png", "manifest.json", "outline.png"]:
            return [f"the package holds {names}, not manifest.json, color.png and outline.png"]
        doc = json.loads(z.read("manifest.json"))
        errors += validate(doc, schema)
        for icon, size in (("color", (192, 192)), ("outline", (32, 32))):
            data = z.read(doc["icons"][icon])
            if png_size(data) != size:
                errors.append(f"{icon}.png is {png_size(data)}, not {size}")
        outline = z.read("outline.png")
        if not _outline_is_white_on_transparent(outline):
            errors.append("outline.png is not white on transparent")
    return errors


def _outline_is_white_on_transparent(data: bytes) -> bool:
    width, height = png_size(data)
    idat = b""
    pos = 8
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        kind = data[pos + 4 : pos + 8]
        if kind == b"IDAT":
            idat += data[pos + 8 : pos + 8 + length]
        pos += 12 + length
    raw = zlib.decompress(idat)
    stride = 1 + 4 * width
    for y in range(height):
        row = raw[y * stride : (y + 1) * stride]
        if row[0] != 0:
            return False  # only unfiltered rows, as written here
        for x in range(width):
            r, g, b, a = row[1 + 4 * x : 5 + 4 * x]
            if a not in (0, 255) or (a == 255 and (r, g, b) != (255, 255, 255)):
                return False
    return True


def build_zip(doc: dict) -> bytes:
    buf = io.BytesIO()
    files = [
        ("manifest.json", (json.dumps(doc, indent=2) + "\n").encode()),
        ("color.png", color_icon()),
        ("outline.png", outline_icon()),
    ]
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files:
            info = zipfile.ZipInfo(name, date_time=ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, data)
    return buf.getvalue()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="package.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--bot-app-id", required=True)
    b.add_argument("--portal-url", required=True)
    b.add_argument("--schema", required=True)
    b.add_argument("--out", required=True)
    b.add_argument("--version", default="1.0.0")
    b.add_argument("--app-id")
    b.add_argument("--name", default="OmniFile")
    b.add_argument("--relay-host")
    v = sub.add_parser("validate")
    v.add_argument("--schema", required=True)
    v.add_argument("path")
    args = p.parse_args(argv)
    schema = json.loads(Path(args.schema).read_text())
    if args.cmd == "validate":
        path = Path(args.path)
        if path.suffix == ".zip":
            errors = check_package(path.read_bytes(), schema)
        else:
            errors = validate(json.loads(path.read_text()), schema)
    else:
        try:
            doc = manifest(
                bot_app_id=args.bot_app_id,
                portal_url=args.portal_url,
                version=args.version,
                app_id=args.app_id,
                name=args.name,
                relay_host=args.relay_host,
            )
        except ValueError as e:
            print(f"build.sh: {e}", file=sys.stderr)
            return 2
        data = build_zip(doc)
        errors = check_package(data, schema)
        if not errors:
            out = Path(args.out)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(data)
            print(
                f"manifest id {doc['id']}, bot {doc['bots'][0]['botId']}, version {doc['version']}"
            )
            print(out)
    for e in errors:
        print(f"invalid: {e}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
