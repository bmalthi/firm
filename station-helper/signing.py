"""Create an owner-only helper signing identity without changing host trust.

Run with uv. Keep this directory outside all images, exports and role paths.
The dedicated keychain uses filesystem protection (0700 directory/0600 files),
not an owner login password. Only codesign is authorized to use its private key.
"""

import argparse
import hashlib
import json
import os
import shlex
import stat
import subprocess
from pathlib import Path

# This public compatibility password is not a security boundary. Owner-only files
# protect the local signing key; code running as the owner can unlock it and sign
# a replacement helper with the identity trusted by an enrolled guest.
LOCAL_PASSWORD = "firm-local-signing"


def run(*argv, **kwargs):
    return subprocess.run(argv, check=True, capture_output=True, **kwargs).stdout


def unlock(root):
    root = root.absolute()
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Signing directory must be owner-only")
    keychain = root / "helper.keychain-db"
    info = keychain.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Signing keychain must be an owner-only regular file")
    run("/usr/bin/security", "unlock-keychain", "-p", LOCAL_PASSWORD, str(keychain))
    return {"unlocked": str(keychain)}


def create(root):
    root = root.absolute()
    if root.exists() or root.is_symlink():
        raise ValueError("Choose a new signing directory; existing identities are never replaced")
    root.mkdir(mode=0o700)
    certificate = root / "certificate.pem"
    key = root / "private.pem"
    archive = root / "identity.p12"
    keychain = root / "helper.keychain-db"
    config = root / "certificate.cnf"
    config.write_text("""[req]
distinguished_name = subject
x509_extensions = extensions
prompt = no
[subject]
CN = Firm Station Helper Local Signing
[extensions]
basicConstraints = critical,CA:false
keyUsage = critical,digitalSignature
extendedKeyUsage = critical,codeSigning
subjectKeyIdentifier = hash
""")
    before = shlex.split(run("/usr/bin/security", "list-keychains", "-d", "user").decode())
    try:
        run(
            "/usr/bin/openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:3072",
            "-nodes",
            "-days",
            "3650",
            "-config",
            str(config),
            "-keyout",
            str(key),
            "-out",
            str(certificate),
        )
        run(
            "/usr/bin/openssl",
            "pkcs12",
            "-export",
            "-inkey",
            str(key),
            "-in",
            str(certificate),
            "-out",
            str(archive),
            "-passout",
            "pass:firm-local-import",
        )
        run("/usr/bin/security", "create-keychain", "-p", LOCAL_PASSWORD, str(keychain))
        run(
            "/usr/bin/security",
            "import",
            str(archive),
            "-k",
            str(keychain),
            "-P",
            "firm-local-import",
            "-T",
            "/usr/bin/codesign",
        )
        run(
            "/usr/bin/security",
            "set-key-partition-list",
            "-S",
            "apple-tool:,apple:,codesign:",
            "-s",
            "-k",
            LOCAL_PASSWORD,
            str(keychain),
        )
        der = run("/usr/bin/openssl", "x509", "-in", str(certificate), "-outform", "DER")
        identity = hashlib.sha1(der).hexdigest().upper()
        result = {
            "identity": identity,
            "keychain": str(keychain),
            "certificate": str(certificate),
            "trust_settings_changed": False,
        }
        (root / "identity.json").write_text(json.dumps(result, indent=2) + "\n")
        return result
    finally:
        # security create-keychain may add its new file to the user's search list.
        # Signing always supplies --keychain, so preserve the previous list.
        after = shlex.split(run("/usr/bin/security", "list-keychains", "-d", "user").decode())
        if after != before:
            run("/usr/bin/security", "list-keychains", "-d", "user", "-s", *before)
        # Keep the only private key in the dedicated keychain, not PEM or P12.
        key.unlink(missing_ok=True)
        archive.unlink(missing_ok=True)
        for path in root.iterdir():
            if stat.S_ISREG(path.lstat().st_mode):
                path.chmod(0o600)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument(
        "--unlock", action="store_true", help="unlock an existing local signing keychain"
    )
    args = parser.parse_args()
    os.umask(0o077)
    try:
        print(
            json.dumps(unlock(args.directory) if args.unlock else create(args.directory), indent=2)
        )
    except subprocess.CalledProcessError as exc:
        raise SystemExit(
            f"Signing identity setup failed: {exc.stderr.decode(errors='replace')}"
        ) from exc
