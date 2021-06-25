#!/usr/bin/env python3

"""
Unofficial Signal for Android backup file decryption utility.

Usage::

    $ python decrypt_backup.py backup_filenme [output_directory] [-p PASSPHRASE]
"""

from typing import NamedTuple, BinaryIO, Iterator, Union, Dict, cast

import sys

import struct

import sqlite3

import json

import getpass

from pathlib import Path

from argparse import ArgumentParser, FileType

from cryptography.hazmat.primitives.hashes import Hash, SHA256, SHA512

from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from cryptography.hazmat.primitives.hmac import HMAC

from cryptography.hazmat.primitives.ciphers import Cipher
from cryptography.hazmat.primitives.ciphers.algorithms import AES
from cryptography.hazmat.primitives.ciphers.modes import CTR

from Backups_pb2 import BackupFrame, SqlStatement  # type: ignore
from Database_pb2 import ChatColor

from google.protobuf.json_format import MessageToJson


class HeaderData(NamedTuple):
    initialisation_vector: bytes  # 16 bytes
    salt: bytes


def read_backup_header(backup_file: BinaryIO) -> HeaderData:
    """Read the header from the start of a Signal backup file."""
    length = struct.unpack(">I", backup_file.read(4))[0]
    backup_frame = BackupFrame.FromString(backup_file.read(length))

    assert backup_frame.HasField("header")
    assert backup_frame.header.HasField("iv")
    assert backup_frame.header.HasField("salt")

    return HeaderData(
        initialisation_vector=backup_frame.header.iv,
        salt=backup_frame.header.salt,
    )


class Keys(NamedTuple):
    cipher_key: bytes  # 32 bytes
    hmac_key: bytes  # 32 bytes


def derive_keys(passphrase: str, salt: bytes) -> Keys:
    """Derive the AES cipher and HMAC keys from a passphrase."""
    passphrase_bytes = passphrase.replace(" ", "").encode("ascii")

    hash = passphrase_bytes
    sha512 = Hash(algorithm=SHA512())
    sha512.update(salt)
    for _ in range(250000):
        sha512.update(hash)
        sha512.update(passphrase_bytes)
        hash = sha512.finalize()
        sha512 = Hash(algorithm=SHA512())

    hkdf = HKDF(algorithm=SHA256(), length=64, info=b"Backup Export", salt=b"")
    keys = hkdf.derive(hash[:32])
    return Keys(
        cipher_key=keys[:32],
        hmac_key=keys[32:],
    )


def increment_initialisation_vector(initialisation_vector: bytes) -> bytes:
    """Increment the counter in the IV."""
    counter = struct.unpack(">I", initialisation_vector[:4])[0]
    counter = (counter + 1) & 0xFFFFFFFF
    return struct.pack(">I", counter) + initialisation_vector[4:]


class MACMismatchError(Exception):
    def __init__(self) -> None:
        super().__init__(
            "Bad MAC found. Passphrase may be incorrect or file corrupted."
        )


def decrypt_frame(
    backup_file: BinaryIO,
    hmac_key: bytes,
    cipher_key: bytes,
    initialisation_vector: bytes,
) -> BackupFrame:
    """Decrypt the next frame in the backup file."""
    length = struct.unpack(">I", backup_file.read(4))[0]
    assert length >= 10
    ciphertext = backup_file.read(length - 10)
    their_mac = backup_file.read(10)

    hmac = HMAC(hmac_key, SHA256())
    hmac.update(ciphertext)
    our_mac = hmac.finalize()
    if their_mac != our_mac[: len(their_mac)]:
        raise MACMismatchError()

    cipher = Cipher(algorithm=AES(cipher_key), mode=CTR(initialisation_vector))
    decryptor = cipher.decryptor()
    frame_bytes = decryptor.update(ciphertext) + decryptor.finalize()

    return BackupFrame.FromString(frame_bytes)


def decrypt_frame_payload(
    backup_file: BinaryIO,
    length: int,
    hmac_key: bytes,
    cipher_key: bytes,
    initialisation_vector: bytes,
    chunk_size: int = 8 * 1024,
) -> Iterator[bytes]:
    """
    Decrypt an encrypted binary payload from the backup file in ``chunk_size`` chunks.
    """
    hmac = HMAC(hmac_key, SHA256())
    hmac.update(initialisation_vector)

    cipher = Cipher(algorithm=AES(cipher_key), mode=CTR(initialisation_vector))
    decryptor = cipher.decryptor()

    # Read the data, incrementally decrypting one chunk at a time
    while length > 0:
        this_chunk_length = min(chunk_size, length)
        length -= this_chunk_length
        ciphertext = backup_file.read(this_chunk_length)

        hmac.update(ciphertext)
        yield decryptor.update(ciphertext)

    # Verify MAC
    their_mac = backup_file.read(10)
    our_mac = hmac.finalize()
    if their_mac != our_mac[: len(their_mac)]:
        raise MACMismatchError()

    # Output final decrypted data
    yield decryptor.finalize()


def parameter_to_native_type(
    parameter: SqlStatement.SqlParameter,
) -> Union[str, int, float, bytes, None]:
    if parameter.HasField("stringParamter"):
        return cast(str, parameter.stringParamter)
    elif parameter.HasField("integerParameter"):
        i = cast(int, parameter.integerParameter)
        # Convert from unsigned to signed integer (for SQLite's benefit)
        if i & (1 << 63):
            i |= -1 << 63
        return i
    elif parameter.HasField("doubleParameter"):
        return cast(float, parameter.doubleParameter)
    elif parameter.HasField("blobParameter"):
        return cast(bytes, parameter.blobParameter)
    elif parameter.HasField("nullparameter"):
        assert parameter.nullparameter is True
        return None
    assert False


def decrypt_backup(
    backup_file: BinaryIO,
    passphrase: str,
    output_directory: Path,
) -> Iterator[None]:
    """
    Decrypt a Signal Android backup file into the specified directory.

    Will create the output directory if it does not exist and may overwrite
    any existing files.

    Creates ``database.sqlite``, ``preferences.json`` and three directories
    ``attachments``, ``stickers`` and ``avatars``. In each of the three
    directories, decrypted files named ``<id>.bin`` will be created.

    Implemented as a generator which yields frequently to allow the display of
    a progress bar (e.g. by using ``backup_file.tell()``.
    """
    database_filename = output_directory / "database.sqlite"
    preferences_filename = output_directory / "preferences.json"
    attachments_directory = output_directory / "attachments"
    stickers_directory = output_directory / "stickers"
    avatars_directory = output_directory / "avatars"
    keyValue_filename = output_directory / "keyValue.json"

    # Create output directories
    for directory in [
        output_directory,
        attachments_directory,
        stickers_directory,
        avatars_directory,
    ]:
        if not directory.is_dir():
            directory.mkdir(parents=True)

    # Create empty DB
    if database_filename.is_file():
        database_filename.unlink()
    db_connection = sqlite3.connect(database_filename)
    db_cursor = db_connection.cursor()

    # Preferences stored as a dictionary {<file>: {<key>: <value>, ...}, ...}
    preferences: Dict[str, Dict[str, str]] = {}
    keyValue = {}

    # Work out basic cryptographic parameters
    initialisation_vector, salt = read_backup_header(backup_file)
    cipher_key, hmac_key = derive_keys(passphrase, salt)

    # Begin decryption, one frame at a time
    while True:
        backup_frame = decrypt_frame(
            backup_file, hmac_key, cipher_key, initialisation_vector
        )
        initialisation_vector = increment_initialisation_vector(initialisation_vector)

        if backup_frame.HasField("end"):
            break
        elif backup_frame.HasField("version"):
            db_cursor.execute(
                f"PRAGMA user_version = {backup_frame.version.version:d}",
            )
        elif backup_frame.HasField("statement"):
            statement = backup_frame.statement
            # Skip SQLite internal tables and full text search index tables
            assert isinstance(statement.statement, str)
            if (
                not statement.statement.lower().startswith("create table sqlite_")
                and "sms_fts_" not in statement.statement
                and "mms_fts_" not in statement.statement
            ):
                db_cursor.execute(
                    statement.statement,
                    tuple(map(parameter_to_native_type, statement.parameters)),
                )
        elif backup_frame.HasField("preference"):
            preference = backup_frame.preference
            preferences.setdefault(preference.file, {})[
                preference.key
            ] = preference.value
        elif backup_frame.HasField("keyValue"):
            key = backup_frame.keyValue.key
            for type in ["blob","boolean","float","integer","long","string"]: # protobufs are awkward because you can't just ask them what type they are; they behave like a union type in C.
                if backup_frame.keyValue.HasField(f"{type}Value"):
                    value = getattr(backup_frame.keyValue, f'{type}Value')
                    break
            else:
                raise ValueError("Unrecognized keyValue type")
            keyValue[key] = value
        else:
            if backup_frame.HasField("attachment"):
                filename = (
                    attachments_directory
                    / f"{backup_frame.attachment.attachmentId}.bin"
                )
                length = backup_frame.attachment.length
            elif backup_frame.HasField("sticker"):
                filename = stickers_directory / f"{backup_frame.sticker.rowId}.bin"
                length = backup_frame.sticker.length
            elif backup_frame.HasField("avatar"):
                filename = avatars_directory / f"{backup_frame.avatar.recipientId}.bin"
                length = backup_frame.avatar.length
            else:
                assert False, "Invalid field type found."

            with open(filename, "wb") as f:
                for data in decrypt_frame_payload(
                    backup_file,
                    length,
                    hmac_key,
                    cipher_key,
                    initialisation_vector,
                ):
                    f.write(data)
            initialisation_vector = increment_initialisation_vector(
                initialisation_vector
            )

        # Yield to allow for e.g. printing progress information.
        yield

    db_connection.commit()

    with preferences_filename.open("w") as pf:
        json.dump(preferences, pf)

    # convert embedded protobufs to something json-compatible
    # this is a dumb format.
    if 'chat_colors.chat_colors' in keyValue:
      B = keyValue['chat_colors.chat_colors']
      b = ChatColor.FromString(B)
      J = MessageToJson(b)
      j = json.loads(J)
      keyValue['chat_colors.chat_colors'] = j
    
# keyValue =
# {'pin.pin_reminders_enabled': True, 'settings.link_previews': False, 'pref_trim_length': 1000, 'pref_trim_threads': True, 'settings.backups.enabled': True, 'settings.universal.expire.timer': 604800, 'chat_colors.chat_colors': b"\x12%\r\xc4\xb4'C\x12\x14\xf2\xe6\x99\xf9\xff\xff\xff\xff\xff\x01\xb2\xd9\x84\xfc\xff\xff\xff\xff\xff\x01\x1a\x08\x00\x00\x00\x00\x00\x00\x80?", 'chat_colors.chat_colors.id': 1, 'chat_colors.auto.tooltip': False, 'chat_colors.gradient.tooltip': False}

    print(keyValue)
    breakpoint()
    
    with keyValue_filename.open("w") as kv:
        json.dump(keyValue, kv)

def main() -> None:
    """Main command-line interface."""
    parser = ArgumentParser(
        description="""
            Decrypt a Signal for Android backup file into its constituent
            SQLite database and associated files (e.g. attachments, stickers
            and avatars).
        """
    )
    parser.add_argument(
        "backup_file",
        type=FileType("rb"),
        help="The Signal for Android backup file.",
    )
    parser.add_argument(
        "output_directory",
        type=Path,
        nargs="?",
        default=Path("./out"),
        help="""
            The output directory into which the decrypted data will be written.
            Defaults to %(default)s.  This directory will be created if it does
            not exist. Existing files may be silently overwritten. Within this
            directory the following will be created. A `database.sqlite`
            containing the Signal app's SQLite database. A `preferences.json`
            file containing certain preference data held by Signal. A trio of
            directories `attachments`, `avatars` and `stickers` which contain
            binary blobs extracted from the backup. These files are named
            according to database IDs in the SQLite database.
        """,
    )
    parser.add_argument(
        "--passphrase",
        "-p",
        help="""
            The backup file passphrase. If this argument is not provided, the
            passphrase will be requested interactively.
        """,
    )
    args = parser.parse_args()

    if args.passphrase is None:
        args.passphrase = getpass.getpass("Backup passphrase: ")

    # Get backup filesize (for progress indication purposes)
    args.backup_file.seek(0, 2)
    backup_file_size = args.backup_file.tell()
    args.backup_file.seek(0)

    last_perc = ""
    try:
        for _ in decrypt_backup(
            args.backup_file, args.passphrase, args.output_directory
        ):
            perc = f"{args.backup_file.tell() * 100 / backup_file_size:0.1f}%"
            if perc != last_perc:
                sys.stderr.write(perc + "\n")
                last_perc = perc
    except MACMismatchError:
        sys.stderr.write("Error: Incorrect passphrase or corrupted backup (Bad MAC)\n")
        sys.exit(1)


if __name__ == "__main__":
    main()