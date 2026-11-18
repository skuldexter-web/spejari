#!/usr/bin/env python3
"""A stand-in for readsb / dump1090: serves SBS lines on --net-sbs-port. Used by the ADS-B tests."""
import socket
import sys
import time

args = sys.argv[1:]
port = int(args[args.index("--net-sbs-port") + 1])
dev = args[args.index("--device") + 1] if "--device" in args else "?"
srv = socket.socket()
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("127.0.0.1", port))
srv.listen(1)
sys.stderr.write(f"fake decoder device {dev} port {port}\n")
sys.stderr.flush()
conn, _ = srv.accept()
icao = "A1B2C" + dev[-1]
n = 0
while True:
    lat, lon = 52.0 + n * 0.01, 21.0 + n * 0.01
    lines = [
        f"MSG,1,1,1,{icao},1,2026/01/01,00:00:00.000,2026/01/01,00:00:00.000,TST{dev}  ,,,,,,,,0,0,0,0",
        f"MSG,3,1,1,{icao},1,2026/01/01,00:00:00.000,2026/01/01,00:00:00.000,,35000,,,{lat},{lon},,,0,0,0,0",
        f"MSG,4,1,1,{icao},1,2026/01/01,00:00:00.000,2026/01/01,00:00:00.000,,,420,90,,,64,,0,0,0,0",
    ]
    try:
        conn.sendall(("\r\n".join(lines) + "\r\n").encode())
    except OSError:
        break
    n += 1
    time.sleep(0.2)
