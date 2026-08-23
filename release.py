#!/usr/bin/env python3
"""Build, sign, verify and package the tracker's `.exe`.

    python release.py                 build -> sign -> verify -> zip -> sha256
    python release.py --check         which signtool and which certificate would
                                      be used, and whether dist/ holds a signed
                                      exe; touches nothing
    python release.py --no-build      sign, verify and zip the exe already in dist/
    python release.py --unsigned      package without signing; says so out loud
                                      and names the zip *-unsigned.zip

The build itself is still `python -m PyInstaller ootmm.spec`. This wraps it so
that the exe that goes into the zip is the one that was signed and verified,
and so that a missing certificate **stops** the release instead of quietly
producing an unsigned zip. That last case is the reason this file exists:
signtool failing to find a certificate is one line that scrolls past, and the
zip looks the same either way.

The certificate is Certum's *Open Source Code Signing* in the cloud
(SimplySign). While SimplySign Desktop is running and logged in it shows up in
the user's certificate store as a virtual smart card, with a subject that
starts with `Open Source Developer`. Anything else needs `--subject` or
`--thumbprint`.

Two independent checks decide that the exe is signed: signtool's own `verify`
(chain, timestamp, who signed) and a look at the PE header (the certificate
table must be there and well-formed). The zip is then reopened and the exe
inside must pass the second check too, so the archive cannot carry a stale
unsigned build. A signature without a timestamp is refused as well: it would
die with the certificate, and Certum's lasts a year. And the signed exe is run
once with `--help`, because a one-file PyInstaller build finds its own archive
by scanning backwards from the end of the file, which is exactly where the
signature goes.
"""

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
EXE = DIST / "ootmm-tracker.exe"
SPEC = ROOT / "ootmm.spec"

# Travel next to the exe in the zip, as {source: name in the zip}. MIT wants
# its notice with every copy, and `data/` is OoTMM's; both are inside the exe
# too (see ootmm.spec), but the zip is what people open first. Both emulator
# scripts go in a Scripts/ folder because each emulator's Lua console opens a
# *file*: tracker.lua for Project64-EM, tracker-bizhawk.lua for BizHawk. They
# are different files (socket vs shared memory), and the exe keeps this folder
# current on startup (see ootmm.scripts_dir).
SHIPPED = {
    "LICENSE": "LICENSE",
    "THIRD-PARTY.md": "THIRD-PARTY.md",
    "Scripts/tracker.lua": "Scripts/tracker.lua",
    "Scripts/tracker-bizhawk.lua": "Scripts/tracker-bizhawk.lua",
}

# Certum issues its open-source certificates to a natural person with this
# fixed prefix in the subject: "Open Source Developer, <name>".
SUBJECT = "Open Source Developer"

# RFC 3161 timestamp servers, tried in this order. Certum's first because it
# is the issuer's; DigiCert's is the usual fallback when it hiccups. A
# timestamp from any of them is fine: it is independent of who issued the
# signing certificate.
TSA = ["http://time.certum.pl", "http://timestamp.digicert.com"]


def say(msg):
    print(f"[release] {msg}", flush=True)


def die(msg, detail=None):
    print(f"[release] ERROR: {msg}", file=sys.stderr, flush=True)
    if detail:
        print(detail.rstrip(), file=sys.stderr, flush=True)
    sys.exit(1)


def version():
    """`__version__` from version.py, without importing the tracker."""
    text = (ROOT / "version.py").read_text(encoding="utf-8")
    m = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.M)
    if not m:
        die("could not read __version__ from version.py")
    return m.group(1)


# --------------------------------------------------------------------------
# tools


def _vertuple(s):
    try:
        return tuple(int(x) for x in s.split("."))
    except ValueError:
        return (0,)


def find_signtool():
    """signtool.exe: $SIGNTOOL, then PATH, then the newest Windows 10/11 SDK."""
    env = os.environ.get("SIGNTOOL")
    if env and Path(env).is_file():
        return Path(env)
    w = shutil.which("signtool")
    if w:
        return Path(w)
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    kits = Path(pf86) / "Windows Kits" / "10"
    found = sorted(kits.glob("bin/*/x64/signtool.exe"), key=lambda p: _vertuple(p.parts[-3]))
    if found:
        return found[-1]
    ack = kits / "App Certification Kit" / "signtool.exe"
    if ack.is_file():
        return ack
    return None


SIGNTOOL_HELP = (
    "signtool.exe was not found. It comes with the Windows SDK ('Windows SDK "
    "Signing Tools for Desktop Apps'), e.g. `winget install "
    "Microsoft.WindowsSDK.10.0.26100`; or point SIGNTOOL at one."
)

# One line per code-signing certificate in the user's personal store. The
# cloud certificate lives there only while SimplySign Desktop is logged in,
# which is why an empty list is a *state* to report, not just an error.
PS_LIST_CERTS = (
    "Get-ChildItem Cert:\\CurrentUser\\My -CodeSigningCert | ForEach-Object { "
    "'{0}|{1}|{2}|{3}' -f $_.Thumbprint, $_.Subject, "
    "$_.NotAfter.ToString('yyyy-MM-dd'), $_.HasPrivateKey }"
)


def list_certs():
    r = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", PS_LIST_CERTS],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        die("could not list the certificate store", r.stderr or r.stdout)
    certs = []
    for line in r.stdout.splitlines():
        parts = line.strip().split("|", 3)
        if len(parts) != 4:
            continue
        thumb, subject, not_after, has_key = parts
        certs.append({
            "thumbprint": thumb.upper(),
            "subject": subject,
            "not_after": not_after,
            "has_key": has_key.strip().lower() == "true",
        })
    return certs


def pick_cert(certs, subject=None, thumbprint=None):
    """The certificate to sign with, or None. Several matches: the one that
    expires last, i.e. the newest — a renewal next to the old one is the
    common case."""
    if thumbprint:
        t = thumbprint.replace(" ", "").upper()
        hits = [c for c in certs if c["thumbprint"] == t]
    else:
        s = (subject or SUBJECT).lower()
        hits = [c for c in certs if s in c["subject"].lower()]
    hits = [c for c in hits if c["has_key"]]
    if not hits:
        return None
    return sorted(hits, key=lambda c: c["not_after"])[-1]


def cert_line(c):
    return f'{c["thumbprint"]}  {c["subject"]}  (until {c["not_after"]}' + (
        ")" if c["has_key"] else ", NO PRIVATE KEY)"
    )


# --------------------------------------------------------------------------
# the PE side: is there a certificate table, and does it look like one?


def pe_signature(data):
    """(signed, detail) from the bytes of a PE file, without any crypto.

    IMAGE_DIRECTORY_ENTRY_SECURITY (index 4) holds a *file offset* and a size;
    at that offset sits a WIN_CERTIFICATE header whose type must be 0x0002
    (PKCS#7). This is the structural half of "is it signed": signtool's verify
    is the cryptographic half, and the two are checked separately on purpose.
    """
    if data[:2] != b"MZ":
        return False, "not an MZ executable"
    pe = int.from_bytes(data[0x3C:0x40], "little")
    if data[pe:pe + 4] != b"PE\0\0":
        return False, "no PE header"
    opt = pe + 24
    magic = int.from_bytes(data[opt:opt + 2], "little")
    if magic == 0x10B:
        dd = opt + 96
    elif magic == 0x20B:
        dd = opt + 112
    else:
        return False, f"unknown optional header magic 0x{magic:x}"
    off = int.from_bytes(data[dd + 32:dd + 36], "little")
    size = int.from_bytes(data[dd + 36:dd + 40], "little")
    if off == 0 or size == 0:
        return False, "no certificate table"
    if off + size > len(data):
        return False, "certificate table points outside the file"
    length = int.from_bytes(data[off:off + 4], "little")
    rev = int.from_bytes(data[off + 4:off + 6], "little")
    typ = int.from_bytes(data[off + 6:off + 8], "little")
    if typ != 0x0002:
        return False, f"certificate type 0x{typ:x} is not PKCS#7"
    if length > size or length < 8:
        return False, f"certificate length {length} does not fit its table of {size}"
    return True, f"certificate table at 0x{off:x}, {size} bytes, revision 0x{rev:x}"


def pe_signature_of(path):
    return pe_signature(Path(path).read_bytes())


# --------------------------------------------------------------------------
# signtool


def sign(signtool, exe, thumbprint, tsas):
    """Sign with an RFC 3161 timestamp, trying each server twice: they do
    fail transiently, and a signature without a timestamp is not one we ship.
    Any error that is not about the timestamp (no key, PIN refused, wrong
    file) is final and reported as is."""
    last = ""
    for tsa in tsas:
        for attempt in (1, 2):
            cmd = [
                str(signtool), "sign", "/fd", "sha256", "/td", "sha256",
                "/tr", tsa, "/sha1", thumbprint, "/v", str(exe),
            ]
            say(f"signing with {tsa} (attempt {attempt}) ...")
            r = subprocess.run(cmd, capture_output=True, text=True)
            out = (r.stdout or "") + (r.stderr or "")
            if r.returncode == 0 and "Successfully signed" in out:
                return out
            last = out
            if "timestamp" not in out.lower():
                die("signtool sign failed", out)
            time.sleep(3)
    die("could not get a timestamp from any server", last)


def verify(signtool, exe):
    """signtool verify /pa /v, parsed: (ok, timestamped, leaf_subject, output).

    /pa is the plain Authenticode policy — the one Windows applies to a
    downloaded exe. The leaf is the last "Issued to:" under "Signing
    Certificate Chain:", which is who the UAC prompt will name.
    """
    r = subprocess.run(
        [str(signtool), "verify", "/pa", "/v", str(exe)],
        capture_output=True, text=True, timeout=300,
    )
    out = (r.stdout or "") + (r.stderr or "")
    ok = r.returncode == 0 and "Successfully verified" in out
    timestamped = bool(re.search(r"signature is timestamped", out, re.I)) and not re.search(
        r"not timestamped", out, re.I
    )
    # signtool prints the chain root first, leaf last, and then the
    # timestamp's own chain with the same "Issued to:" lines: cut the block
    # before that and take the last entry.
    leaf = None
    chain = out.find("Signing Certificate Chain:")
    if chain >= 0:
        block = out[chain:]
        for marker in ("The signature is timestamped", "Timestamp Verified by:",
                       "File is not timestamped", "Successfully verified",
                       "SignTool Error", "Number of "):
            i = block.find(marker)
            if i > 0:
                block = block[:i]
        hits = re.findall(r"Issued to:\s*(.+)", block)
        if hits:
            leaf = hits[-1].strip()
    return ok, timestamped, leaf, out


def smoke(exe):
    """Run the finished exe once. `--help` makes argparse print the usage and
    exit 0, and the one-file bootloader has to unpack and start Python to get
    there — which is what a signature appended in the wrong place breaks."""
    r = subprocess.run([str(exe), "--help"], capture_output=True, text=True, timeout=180)
    out = (r.stdout or "") + (r.stderr or "")
    return r.returncode == 0 and "usage:" in out.lower(), out


# --------------------------------------------------------------------------
# build and package


def kill_running():
    """A running ootmm-tracker.exe holds dist\\ and PyInstaller fails with
    PermissionError. The one-file build spawns a child, so kill by name."""
    r = subprocess.run(["taskkill", "/F", "/IM", EXE.name], capture_output=True, text=True)
    if r.returncode == 0:
        say(f"killed running {EXE.name}")
    # 128 = no such process; anything else (e.g. access denied) surfaces on
    # its own when PyInstaller cannot write dist\.


def build():
    started = time.time()
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", str(SPEC)]
    say("building: " + " ".join(cmd))
    r = subprocess.run(cmd, cwd=ROOT)
    if r.returncode != 0:
        die(f"PyInstaller exited with {r.returncode}")
    if not EXE.is_file() or EXE.stat().st_mtime < started:
        die(f"{EXE} was not produced by this build")
    say(f"built {EXE.name}, {EXE.stat().st_size:,} bytes")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def package(ver, signed):
    """Zip the exe with LICENSE and THIRD-PARTY.md, then reopen the zip and
    check that the exe *inside* is (or is not) signed, as expected. Returns
    the zip path."""
    suffix = "" if signed else "-unsigned"
    zpath = DIST / f"ootmm-tracker-{ver}-win64{suffix}.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for src, name in SHIPPED.items():
            z.write(ROOT / src, name)
        z.write(EXE, EXE.name)
    with zipfile.ZipFile(zpath) as z:
        bad = z.testzip()
        if bad:
            die(f"zip is corrupt at {bad}")
        names = z.namelist()
        inside, detail = pe_signature(z.read(EXE.name))
    missing = [n for n in list(SHIPPED.values()) + [EXE.name] if n not in names]
    if missing:
        die(f"zip is missing {missing}")
    if inside != signed:
        die(
            f"the exe inside {zpath.name} is {'signed' if inside else 'NOT signed'} "
            f"but the release was meant to be {'signed' if signed else 'unsigned'} ({detail})"
        )
    say(f"packaged {zpath.name}: {', '.join(names)}, {zpath.stat().st_size:,} bytes")
    return zpath


def write_sums(zpath):
    sums = zpath.with_suffix(".sha256")
    lines = [f"{sha256(p)}  {p.name}" for p in (zpath, EXE)]
    sums.write_text("\n".join(lines) + "\n", encoding="ascii")
    # The "built ... bytes" line above is the exe before the signature was
    # appended; these are the sizes the release notes should quote.
    for line, p in zip(lines, (zpath, EXE)):
        say(f"sha256 {line}  ({p.stat().st_size:,} bytes)")
    say(f"wrote {sums.name}")


# --------------------------------------------------------------------------


def check(args):
    """`--check`: report the tools, the certificates and the exe in dist/."""
    st = find_signtool()
    say(f"signtool: {st if st else 'NOT FOUND - ' + SIGNTOOL_HELP}")
    certs = list_certs()
    if not certs:
        say("code-signing certificates in Cert:\\CurrentUser\\My: none")
        say("  (the Certum cloud certificate is only there while SimplySign Desktop is running and logged in)")
    else:
        say(f"code-signing certificates in Cert:\\CurrentUser\\My: {len(certs)}")
        for c in certs:
            say("  " + cert_line(c))
    chosen = pick_cert(certs, args.subject, args.thumbprint)
    want = f"thumbprint {args.thumbprint}" if args.thumbprint else f'subject containing "{args.subject or SUBJECT}"'
    say(f"would sign with ({want}): {cert_line(chosen) if chosen else 'NOTHING - release.py would stop here'}")
    say(f"timestamp servers: {', '.join(args.tsa or TSA)}")
    if EXE.is_file():
        signed, detail = pe_signature_of(EXE)
        say(f"{EXE.relative_to(ROOT)}: {EXE.stat().st_size:,} bytes, {'SIGNED' if signed else 'not signed'} ({detail})")
        if signed and st:
            ok, ts, leaf, _ = verify(st, EXE)
            say(f"  signtool verify: {'OK' if ok else 'FAILED'}, "
                f"{'timestamped' if ts else 'NO TIMESTAMP'}, signed by {leaf!r}")
    else:
        say(f"{EXE.relative_to(ROOT)}: not built")
    return 0 if (st and chosen) else 1


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--check", action="store_true", help="report tools, certificates and dist/; change nothing")
    p.add_argument("--no-build", action="store_true", help="skip PyInstaller; use the exe already in dist/")
    p.add_argument("--unsigned", action="store_true", help="package without signing (zip is named *-unsigned.zip)")
    p.add_argument("--subject", help=f'substring of the certificate subject (default "{SUBJECT}")')
    p.add_argument("--thumbprint", help="SHA-1 thumbprint of the certificate, instead of --subject")
    p.add_argument("--tsa", action="append", help="timestamp server; repeatable (default: Certum, then DigiCert)")
    args = p.parse_args()

    if args.check:
        sys.exit(check(args))

    ver = version()
    say(f"version {ver}" + (" - UNSIGNED build, on request" if args.unsigned else ""))
    DIST.mkdir(exist_ok=True)

    # Everything that can refuse, refuses before the build: PyInstaller takes
    # a while, and finding out afterwards that there is no certificate is
    # how an unsigned zip ends up looking finished.
    signtool = cert = None
    if not args.unsigned:
        signtool = find_signtool()
        if not signtool:
            die(SIGNTOOL_HELP)
        cert = pick_cert(list_certs(), args.subject, args.thumbprint)
        if not cert:
            die(
                "no usable code-signing certificate "
                + (f"with thumbprint {args.thumbprint}" if args.thumbprint
                   else f'whose subject contains "{args.subject or SUBJECT}"')
                + " in Cert:\\CurrentUser\\My.\n"
                "  Is SimplySign Desktop running and logged in? `python release.py --check` lists what is there;\n"
                "  --subject / --thumbprint pick another certificate; --unsigned packages without signing."
            )
        say(f"signtool: {signtool}")
        say(f"certificate: {cert_line(cert)}")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        if "is not signed" in readme:
            say("WARNING: README.md still says the exe is not signed, and it travels inside the exe")

    if args.no_build:
        if not EXE.is_file():
            die(f"{EXE} does not exist; drop --no-build")
        say(f"using the existing {EXE.relative_to(ROOT)} ({EXE.stat().st_size:,} bytes)")
    else:
        kill_running()
        build()

    if args.unsigned:
        signed, detail = pe_signature_of(EXE)
        say(f"not signing; the exe is {'already SIGNED' if signed else 'unsigned'} ({detail})")
        if signed:
            die("an --unsigned release cannot ship a signed exe; rebuild without --no-build")
    else:
        sign(signtool, EXE, cert["thumbprint"], args.tsa or TSA)
        ok, ts, leaf, out = verify(signtool, EXE)
        if not ok:
            die("signtool verify rejected the signed exe", out)
        if not ts:
            die("the signature carries no timestamp; it would expire with the certificate", out)
        want = (args.subject or SUBJECT).lower()
        if not args.thumbprint and (leaf is None or want not in leaf.lower()):
            die(f'the exe is signed by {leaf!r}, not by a certificate matching "{args.subject or SUBJECT}"', out)
        say(f"signtool verify: OK, timestamped, signed by {leaf!r}")
        present, detail = pe_signature_of(EXE)
        if not present:
            die(f"the PE header disagrees with signtool: {detail}")
        say(f"PE certificate table: {detail}")
        alive, out = smoke(EXE)
        if not alive:
            die("the signed exe does not run (`--help` failed)", out)
        say("smoke run: `--help` prints the usage and exits 0")

    zpath = package(ver, signed=not args.unsigned)
    write_sums(zpath)
    say("done. Still by hand: the guards in DEVELOPING.md (code vs exe on a dump, panels, hard link), then")
    say(f"  gh release create v{ver} {zpath.relative_to(ROOT)}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
