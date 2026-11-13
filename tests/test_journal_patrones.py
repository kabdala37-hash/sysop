"""Los patrones de OOM y de errores de disco, contra mensajes reales de kernel.

Un patron que no matchea es un control que nunca dispara y que en el panel se ve
igual de verde que uno que funciona. Es el modo de falla mas silencioso que tiene
un observador, asi que se prueba con texto literal de kernel.
"""
from sysop.adapters.so_linux.collectors.journal import PATRONES_IO, PATRONES_OOM, coincide

OOM_REALES = [
    "Out of memory: Killed process 2417 (node) total-vm:1284custom",
    "node invoked oom-killer: gfp_mask=0x140dca(GFP_HIGHUSER_MOVABLE|__GFP_COMP|__GFP_ZERO), order=0",
    "Memory cgroup out of memory: Killed process 998 (python3) total-vm:512000kB",
    "oom_reaper: reaped process 2417 (node), now anon-rss:0kB",
]

IO_REALES = [
    "EXT4-fs error (device nvme0n1p1): ext4_find_entry:1455: inode #2: comm ls: reading directory lblock 0",
    "Buffer I/O error on dev sda1, logical block 8, async page read",
    "blk_update_request: I/O error, dev sda, sector 12345678 op 0x0:(READ)",
    "EXT4-fs (nvme0n1p1): Remounting filesystem read-only",
    "critical medium error, dev sda, sector 1234",
    "ata1.00: failed command: READ FPDMA QUEUED",
]

NO_SON_ERRORES = [
    "systemd[1]: Started Session 42 of user root.",
    "kernel: usb 1-1: new high-speed USB device number 5",
    "audit: type=1400 audit(1234.567:89): apparmor=\"STATUS\"",
    "plataforma-api[123]: GET /api/calls 200 in 45ms",
    # Este importa: dice "error" pero no es un error de disco. Si matcheara,
    # cada request fallida del back abriria un incidente critico de hardware.
    "nginx: 2026/08/27 [error] 123#0: *45 upstream timed out",
]


def main():
    r = []
    print("=== OOM ===")
    for m in OOM_REALES:
        p = coincide(m, PATRONES_OOM)
        r.append(p is not None)
        print("  %s %-22s %s" % ("OK  " if p else "MAL ", p or "no matchea", m[:58]))

    print("\n=== errores de disco / filesystem ===")
    for m in IO_REALES:
        p = coincide(m, PATRONES_IO)
        r.append(p is not None)
        print("  %s %-32s %s" % ("OK  " if p else "MAL ", p or "no matchea", m[:48]))

    print("\n=== falsos positivos: NO tienen que matchear ===")
    for m in NO_SON_ERRORES:
        pio = coincide(m, PATRONES_IO)
        poom = coincide(m, PATRONES_OOM)
        limpio = pio is None and poom is None
        r.append(limpio)
        print("  %s %-46s %s" % ("OK  " if limpio else "MAL ", m[:46],
                                 "" if limpio else "matcheo %s" % (pio or poom)))

    print("\n%d/%d" % (sum(r), len(r)))
    return 0 if all(r) else 1


if __name__ == "__main__":
    raise SystemExit(main())
