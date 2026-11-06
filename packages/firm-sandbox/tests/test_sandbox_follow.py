"""Following a run's log returns every byte the guest retained."""

from firm_sandbox import Run, Sandbox

RESULT = b'{"duration_api_ms":1,"result":"' + b"x" * 2048 + b'","type":"result"}\n'


def test_follow_drains_output_written_between_empty_read_and_exit():
    # The guest writes the final result line and publishes exit while the host is
    # between an empty log read and its status request.
    log = bytearray(b'{"type":"system","subtype":"init"}\n')
    box = object.__new__(Sandbox)

    def log_chunk(_run, *, offset=0, limit=1024):
        return bytes(log[offset : offset + limit])

    def status(_run):
        log.extend(RESULT)
        return {"running": False, "exit_code": 0}

    box._log_chunk = log_chunk
    box.status = status

    output = b"".join(box.follow(Run("s-1", "run-1", "a" * 32)))

    assert output == bytes(log)
    assert output.endswith(RESULT)
