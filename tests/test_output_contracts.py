"""Preimplementation archive acceptance contract; no extraction or project commands."""

import gzip
import hashlib
import io
import tarfile

import pytest
from hogwarts.outputs.archive import validate_archive
from hogwarts.outputs.contracts import MAX_ARCHIVE, MAX_FILE, MAX_FILES, OutputError


def archive(entries=(), *, format=tarfile.USTAR_FORMAT):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w", format=format) as stream:
        for name, content, kind in entries:
            entry = tarfile.TarInfo(name)
            entry.type = kind
            entry.mode = 0o755 if kind == tarfile.DIRTYPE else 0o644
            if kind in {tarfile.SYMTYPE, tarfile.LNKTYPE}:
                entry.linkname = "../escape"
            entry.size = len(content) if kind == tarfile.REGTYPE else 0
            stream.addfile(entry, io.BytesIO(content))
    return data.getvalue()


def test_normal_and_empty():
    assert validate_archive(archive(), "src", ("src",)) == ()
    data = archive(
        [
            (".executed", b"", tarfile.REGTYPE),
            ("sub", b"", tarfile.DIRTYPE),
            ("sub/prototype.txt", b"\x00hello", tarfile.REGTYPE),
        ]
    )
    result = validate_archive(data, "src", ("src",))
    assert len(result) == 1 and result[0].target_path == "src/sub/prototype.txt"
    assert result[0].sha256 == hashlib.sha256(b"\x00hello").hexdigest()


@pytest.mark.parametrize(
    "path",
    [
        "../escape",
        "/absolute",
        "C:/secret",
        "x\\y",
        "a/../b",
        ".git/config",
        ".gitattributes",
        "a//b",
        "a/./b",
        "CON",
        "file.",
        "x:y",
    ],
)
def test_invalid_paths(path):
    with pytest.raises(OutputError):
        validate_archive(archive([(path, b"bad", tarfile.REGTYPE)]), "src", ("src",))


@pytest.mark.parametrize(
    "kind",
    [
        tarfile.SYMTYPE,
        tarfile.LNKTYPE,
        tarfile.FIFOTYPE,
        tarfile.CHRTYPE,
        tarfile.BLKTYPE,
        tarfile.GNUTYPE_SPARSE,
        tarfile.XHDTYPE,
        tarfile.XGLTYPE,
        tarfile.GNUTYPE_LONGNAME,
        tarfile.GNUTYPE_LONGLINK,
    ],
)
def test_links_special_extensions_denied(kind):
    with pytest.raises(OutputError):
        validate_archive(archive([("bad", b"", kind)]), "src", ("src",))


@pytest.mark.parametrize(
    "paths", [("a", "a"), ("a/", "a/"), ("A/file", "a/other"), ("a", "a/b"), ("a/b", "a")]
)
def test_duplicate_and_alias_structure(paths):
    entries = [(p, b"", tarfile.DIRTYPE if p.endswith("/") else tarfile.REGTYPE) for p in paths]
    with pytest.raises(OutputError):
        validate_archive(archive(entries), "src", ("src",))


@pytest.mark.parametrize(
    "change", ["checksum", "padding", "truncated", "trailer", "gzip", "oversize"]
)
def test_malformed_or_bomb_archive(change):
    original = archive([("a", b"abc", tarfile.REGTYPE)])
    data = bytearray(original)
    if change == "checksum":
        data[0] ^= 1
    elif change == "padding":
        data[515] = 1
    elif change == "truncated":
        data = data[:600]
    elif change == "trailer":
        data += original
    elif change == "gzip":
        data = gzip.compress(original)
    else:
        data = b"\0" * (MAX_ARCHIVE + 512)
    with pytest.raises(OutputError):
        validate_archive(bytes(data), "src", ("src",))


def test_size_count_and_grant_caps():
    for data in [
        archive([("big", b"x" * (MAX_FILE + 1), tarfile.REGTYPE)]),
        archive([(f"f{i}", b"", tarfile.REGTYPE) for i in range(MAX_FILES + 1)]),
    ]:
        with pytest.raises(OutputError):
            validate_archive(data, "src", ("src",))
    with pytest.raises(OutputError):
        validate_archive(archive([("no.txt", b"x", tarfile.REGTYPE)]), "src", ("src/yes.txt",))
    with pytest.raises(OutputError):
        validate_archive(archive([(".executed", b"forged", tarfile.REGTYPE)]), "src", ("src",))


@pytest.mark.parametrize(
    "kind", ["total", "entries", "directory_marker", "setuid", "noncanonical_name"]
)
def test_additional_resource_and_structure_limits(kind):
    if kind == "total":
        data = archive([(str(i), b"x" * (3 * 1024 * 1024), tarfile.REGTYPE) for i in range(3)])
    elif kind == "entries":
        data = archive([(f"d{i}", b"", tarfile.DIRTYPE) for i in range(513)])
    elif kind == "directory_marker":
        data = archive([(".executed", b"", tarfile.DIRTYPE)])
    else:
        changed = bytearray(archive([("file", b"x", tarfile.REGTYPE)]))
        if kind == "setuid":
            changed[100:108] = b"0004755\0"
        else:
            changed[0:9] = b"file\0evil"
        changed[148:156] = b"        "
        changed[148:156] = (f"{sum(changed[:512]):06o}\0 ").encode()
        data = bytes(changed)
    with pytest.raises(OutputError):
        validate_archive(data, "src", ("src",))
