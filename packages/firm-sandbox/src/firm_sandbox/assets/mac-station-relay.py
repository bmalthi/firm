"""Host-supplied relay: only the control account can reach the signed station helper."""

import json
import os
import select
import socket
import subprocess
import sys


def main():
    if os.getuid() != 501:
        raise PermissionError("Station relay requires the control account")
    if (len(sys.argv) not in (2, 3) or not sys.argv[1].isdigit()
            or not 1 <= int(sys.argv[1]) <= 8
            or (len(sys.argv) == 3 and sys.argv[2] != "--status")):
        raise ValueError("Expected station number and optional --status")
    station = int(sys.argv[1])
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(10)
        connection.connect(f"/Users/Shared/firm-station-helper/firmstation{station}/station.sock")
        # Verify the kernel-supplied audit token against the installed helper's
        # signature and the selected account, not just any process of that uid.
        subprocess.run(
            ["/Applications/Firm Station Helper.app/Contents/MacOS/FirmStationHelper",
             "--verify-peer", str(connection.fileno()), str(501 + station)],
            pass_fds=(connection.fileno(),), check=True, timeout=10,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if len(sys.argv) == 3:
            connection.sendall(b'{"id":1,"op":"status"}\n')
            reply = b""
            while b"\n" not in reply:
                chunk = connection.recv(65536)
                if not chunk or len(reply) + len(chunk) > 1024 * 1024:
                    raise RuntimeError("Invalid helper status reply")
                reply += chunk
            print(json.dumps(json.loads(reply.split(b"\n", 1)[0])))
            return
        connection.settimeout(None)
        # The host reads this before it asks anything: a relay that ends earlier failed
        # to reach the helper, and the host may simply connect again.
        sys.stdout.buffer.write(b'{"relay":"connected"}\n')
        sys.stdout.buffer.flush()
        while True:
            ready, _, _ = select.select([connection, sys.stdin.buffer], [], [])
            if sys.stdin.buffer in ready:
                data = os.read(sys.stdin.fileno(), 65536)
                if not data:
                    return
                connection.sendall(data)
            if connection in ready:
                data = connection.recv(65536)
                if not data:
                    return
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
