"""Fixed computation entrypoint. No uploaded code is evaluated; no model credentials."""

import base64
import ctypes
import errno
import json
import os
import resource
import sys


def restrict_files(workspace):
    """Landlock denies all filesystem access except runtime reads and scratch writes."""
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    abi = libc.syscall(444, 0, 0, 1)
    if abi < 1:
        raise RuntimeError("Landlock ABI >=1 is required for analysis isolation")

    class Ruleset(ctypes.Structure):
        _fields_ = [("handled", ctypes.c_uint64)]

    class PathRule(ctypes.Structure):
        _pack_ = 1
        _fields_ = [("allowed", ctypes.c_uint64), ("parent", ctypes.c_int32)]

    rights = (1 << (15 if abi >= 3 else 14 if abi >= 2 else 13)) - 1
    attributes = Ruleset(rights)
    descriptor = libc.syscall(444, ctypes.byref(attributes), ctypes.sizeof(attributes), 0)
    if descriptor < 0:
        raise RuntimeError("cannot create analysis filesystem policy")
    try:
        from pathlib import Path

        allowed = {
            "/usr",
            "/lib",
            "/lib64",
            "/etc/fonts",
            sys.prefix,
            sys.base_prefix,
            str(Path(__file__).resolve().parents[1]),
            str(Path(__import__("ingest").__file__).resolve().parent),
        }
        for path in allowed | {workspace}:
            if not os.path.exists(path):
                continue
            parent = os.open(path, os.O_PATH | os.O_CLOEXEC)
            try:
                rule = PathRule(rights if path == workspace else 1 | 4 | 8, parent)
                if libc.syscall(445, descriptor, 1, ctypes.byref(rule), 0) < 0:
                    raise RuntimeError("cannot add analysis filesystem rule")
            finally:
                os.close(parent)
        if libc.prctl(38, 1, 0, 0, 0) or libc.syscall(446, descriptor, 0):
            raise RuntimeError("cannot restrict analysis filesystem")
    finally:
        os.close(descriptor)


def restrict():
    resource.setrlimit(resource.RLIMIT_CPU, (45, 45))
    resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))
    resource.setrlimit(resource.RLIMIT_FSIZE, (32 * 1024**2, 32 * 1024**2))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    # Network and process creation fail closed inside the computation process.
    lib = ctypes.CDLL("libseccomp.so.2")
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    lib.seccomp_load.argtypes = [ctypes.c_void_p]
    lib.seccomp_release.argtypes = [ctypes.c_void_p]
    ctx = lib.seccomp_init(0x7FFF0000)
    if not ctx:
        raise RuntimeError("seccomp unavailable")
    try:
        for name in (
            b"socket",
            b"connect",
            b"execve",
            b"execveat",
            b"fork",
            b"vfork",
            b"truncate",
            b"ftruncate",
        ):
            number = lib.seccomp_syscall_resolve_name(name)
            if number >= 0 and lib.seccomp_rule_add(ctx, 0x50000 | errno.EPERM, number, 0):
                raise RuntimeError("seccomp rule failed")

        class Compare(ctypes.Structure):
            _fields_ = [
                ("arg", ctypes.c_uint),
                ("op", ctypes.c_int),
                ("mask", ctypes.c_uint64),
                ("value", ctypes.c_uint64),
            ]

        lib.seccomp_rule_add_array.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_int,
            ctypes.c_uint,
            ctypes.POINTER(Compare),
        ]
        # Landlock ABI 1 does not mediate truncation: close that gap in seccomp.
        for syscall, argument in ((b"open", 1), (b"openat", 2)):
            condition = Compare(argument, 7, os.O_TRUNC, os.O_TRUNC)
            if lib.seccomp_rule_add_array(
                ctx,
                0x50000 | errno.EPERM,
                lib.seccomp_syscall_resolve_name(syscall),
                1,
                ctypes.byref(condition),
            ):
                raise RuntimeError("cannot restrict file truncation")
        if lib.seccomp_load(ctx):
            raise RuntimeError("seccomp load failed")
    finally:
        lib.seccomp_release(ctx)


def main():
    workspace = os.environ["ANALYSIS_WORKDIR"]
    os.environ["MPLCONFIGDIR"] = workspace
    # Preload native modules before restricting syscalls.
    import matplotlib.font_manager  # noqa: F401
    from ingest.research import analyze, chart_png, read_table

    restrict_files(workspace)
    restrict()
    data = json.load(sys.stdin)
    table = read_table(
        base64.b64decode(data["content"]), data["filename"], data["spec"].get("sheet")
    )
    if data.get("inspect"):
        result = {k: v for k, v in table.items() if k != "rows"}
        result["row_count"] = len(table.get("rows", []))
    else:
        result = analyze(table, data["spec"])
        result["chart_base64"] = base64.b64encode(chart_png(result)).decode()
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False))
        sys.exit(1)
