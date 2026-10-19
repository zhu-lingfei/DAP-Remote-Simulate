"""Unacknowledged benchmark stream and local serial baudrate scan.

Only controls have full CRC/retries. Test data has header CRC only, no block ACK,
no retransmission, and a final length + SHA-256 comparison. Not a file upload API.
"""
import contextlib
import hashlib
import os
import random
import secrets
import tempfile
import time
from pathlib import Path

from .protocol import BENCH_HEADER, Decoder, Frame, Kind, MAX_PAYLOAD, Op

MAX_BENCH_SIZE = 16 * 1024 * 1024
DATA_SIZE = MAX_PAYLOAD - BENCH_HEADER.size


class BenchmarkCancelled(RuntimeError):
    pass


class _ProgressError(Exception):
    def __init__(self, original):
        self.original = original


class BenchmarkReceiver:
    """Streaming receiver used by the simulator and documented as the DAP model."""
    def __init__(self, owner, size, expected_hash, sink="memory", temp_path=None, check_write=None):
        if type(size) is not int or not 1 <= size <= MAX_BENCH_SIZE:
            raise ValueError("benchmark size must be 1..16 MiB")
        if len(expected_hash) != 32 or sink not in ("memory", "sd"):
            raise ValueError("invalid benchmark SHA-256 or sink")
        self.owner, self.size, self.expected_hash = owner, size, expected_hash
        self.sink, self.path, self.check_write = sink, temp_path, check_write
        self.bench_id = secrets.randbits(64) or 1
        self.state, self.error = "ACTIVE", ""
        self.received, self.next_sequence, self.sequence_errors = 0, 0, 0
        self.hasher = hashlib.sha256()
        self.started = self.last_active = time.perf_counter_ns()
        self.elapsed_us = self.verify_us = self.framing_errors = 0
        self.output = open(temp_path, "xb") if sink == "sd" else None
        self.final_hash = b""

    def feed(self, payload):
        if self.state != "ACTIVE":
            return
        if not BENCH_HEADER.size < len(payload) <= MAX_PAYLOAD:
            self.sequence_errors += 1
            return
        bench_id, sequence = BENCH_HEADER.unpack_from(payload)
        if bench_id != self.bench_id:
            self.sequence_errors += 1
            return
        if sequence != self.next_sequence:
            self.sequence_errors += 1
        self.next_sequence = sequence + 1
        data = payload[BENCH_HEADER.size:]
        self.received += len(data)
        self.last_active = time.perf_counter_ns()
        # Hash arrival order, including duplicates. Never repair the test silently.
        if self.received > self.size + MAX_PAYLOAD:
            self.error = "received data exceeds test size limit"
            self.finish()
            return
        self.hasher.update(data)
        if self.output is not None and not self.error:
            try:
                if self.check_write:
                    self.check_write(len(data))
                if self.output.write(data) != len(data):
                    raise OSError("short benchmark SD write")
                self.output.flush()
            except Exception as exc:
                self.error = "SD write failed: " + str(exc)

    def finish(self, framing_errors=0, state=None):
        if self.state != "ACTIVE":
            return self.result()
        self.elapsed_us = max(1, (time.perf_counter_ns() - self.started) // 1000)
        self.framing_errors = framing_errors
        verify_start = time.perf_counter_ns()
        self.final_hash = self.hasher.digest()
        try:
            if self.output is not None:
                self.output.flush()
                os.fsync(self.output.fileno())
                self.output.close()
                self.output = None
                digest = hashlib.sha256()
                with open(self.path, "rb") as source:
                    for data in iter(lambda: source.read(65536), b""):
                        digest.update(data)
                self.final_hash = digest.digest()
        except OSError as exc:
            self.error = "SD verification failed: " + str(exc)
        finally:
            if self.output is not None:
                self.output.close()
                self.output = None
            if self.path:
                Path(self.path).unlink(missing_ok=True)
        self.verify_us = max(1, (time.perf_counter_ns() - verify_start) // 1000)
        passed = (not self.error and self.received == self.size and
                  self.final_hash == self.expected_hash and not self.sequence_errors and not framing_errors)
        self.state = state or ("PASSED" if passed else "FAILED")
        if self.state != "PASSED" and not self.error:
            self.error = "test cancelled/expired" if state else "length, SHA-256, sequence or framing mismatch"
        return self.result()

    def result(self):
        return dict(bench_id=self.bench_id, state=self.state, sink=self.sink, size=self.size,
                    received_bytes=self.received, sha256=self.final_hash or self.hasher.digest(),
                    elapsed_us=self.elapsed_us, verify_us=self.verify_us,
                    passed=self.state == "PASSED", sequence_errors=self.sequence_errors,
                    framing_errors=self.framing_errors, error=self.error)


@contextlib.contextmanager
def _source(size, file_path, seed):
    if file_path is not None:
        with open(file_path, "rb") as source:
            source.seek(0, os.SEEK_END)
            size = source.tell()
            source.seek(0)
            if not 1 <= size <= MAX_BENCH_SIZE:
                raise ValueError("test file must contain 1..16 MiB")
            digest = hashlib.sha256()
            for data in iter(lambda: source.read(65536), b""):
                digest.update(data)
            source.seek(0)
            yield source, size, digest.digest()
    else:
        if type(size) is not int or not 1 <= size <= MAX_BENCH_SIZE:
            raise ValueError("benchmark size must be 1..16 MiB")
        # Prepare data before timing; bounded memory and no generation bottleneck on wire.
        rng, digest = random.Random(seed), hashlib.sha256()
        with tempfile.TemporaryFile() as source:
            remaining = size
            while remaining:
                data = rng.randbytes(min(65536, remaining))
                source.write(data)
                digest.update(data)
                remaining -= len(data)
            source.seek(0)
            yield source, size, digest.digest()


def run_benchmark(client, size=65536, file_path=None, sink="memory", seed=1,
                  progress=None, cancel_event=None):
    if sink not in ("memory", "sd"):
        raise ValueError("sink must be memory or sd")
    with client.lock, _source(size, file_path, seed) as (source, size, expected_hash):
        if cancel_event and cancel_event.is_set():
            raise BenchmarkCancelled("benchmark cancelled")
        result = client.call(Op.BENCH_BEGIN, size=size, sha256=expected_hash, sink=sink)
        bench_id = result["bench_id"]
        started = time.perf_counter()
        try:
            sent, sequence = 0, 0
            while sent < size:
                if cancel_event and cancel_event.is_set():
                    raise BenchmarkCancelled("benchmark cancelled")
                data = source.read(min(DATA_SIZE, size - sent))
                if not data:
                    raise ValueError("benchmark source file changed during test")
                payload = BENCH_HEADER.pack(bench_id, sequence) + data
                client.transport.write(Frame(Kind.BENCH_DATA, 1, client.session, 0,
                                             Op.BENCH_DATA, payload, flags=1).encode())
                sent += len(data)
                sequence += 1
                if progress:
                    progress(dict(event="data", sent_bytes=sent, total_bytes=size))
            send_seconds = time.perf_counter() - started
            # Extra boundary allows END recovery if a data frame's final delimiter was lost.
            client.transport.write(b"\0")
            result = client.call(Op.BENCH_END, bench_id=bench_id)
            result.update(sent_bytes=sent, expected_sha256=expected_hash.hex(),
                          received_sha256=result["sha256"].hex(),
                          bytes_per_s=result["received_bytes"] * 1000000 / max(1, result["elapsed_us"]),
                          send_seconds=send_seconds, total_seconds=time.perf_counter() - started)
            # Never trust a peer's 'passed' flag without checking its reported digest/length.
            result["passed"] = (result["passed"] and result["sha256"] == expected_hash and
                                result["received_bytes"] == size and not result["sequence_errors"] and
                                not result["framing_errors"])
            if progress:
                progress(dict(event="complete", **result))
            return result
        except BaseException:
            with contextlib.suppress(Exception):
                client.transport.write(b"\0")
                client.call(Op.BENCH_ABORT, bench_id=bench_id)
            raise


def scan_baudrates(client, baudrates, rounds=3, size=65536, file_path=None, sink="memory",
                   probe_timeout=0.5, settle_s=0.1, progress=None, cancel_event=None):
    supplied_rates = list(baudrates)
    if (not supplied_rates or any(type(rate) is not int or not 1 <= rate <= 4000000 for rate in supplied_rates)
            or type(rounds) is not int or not 1 <= rounds <= 20
            or not 0.05 <= probe_timeout <= 10 or not 0 <= settle_s <= 5):
        raise ValueError("invalid rates, rounds, probe timeout or settling time")
    rates = sorted(set(supplied_rates))
    if len(rates) > 32:
        raise ValueError("at most 32 baudrate candidates allowed")
    if sink not in ("memory", "sd") or (file_path is None and not 1 <= size <= MAX_BENCH_SIZE):
        raise ValueError("invalid benchmark size or sink")
    # Validate a local file before changing the serial port.
    if file_path is not None and not 1 <= Path(file_path).stat().st_size <= MAX_BENCH_SIZE:
        raise ValueError("test file must contain 1..16 MiB")
    supports = bool(getattr(client.transport, "supports_baudrate", False))
    original = getattr(client.transport, "baudrate", None)
    if supports and (type(original) is not int or original <= 0):
        raise ValueError("serial transport must expose its original integer baudrate")
    summary = dict(simulated=not supports, original_baudrate=original, results=[], cancelled=False,
                   restored=True, restore_error="", highest_passed_baudrate=None,
                   recommended_baudrate=None)
    original_timeout, original_retries = client.timeout, client.retries

    def notify(event):
        if progress:
            try:
                progress(event)
            except Exception as exc:
                raise _ProgressError(exc) from exc

    def sync():
        reset = getattr(client.transport, "reset_input", None)
        if reset:
            reset()
        client.decoder = Decoder()
        client.transport.write(b"\0")
        client.call(Op.PING)

    with client.lock:
        try:
            client.timeout, client.retries = probe_timeout, 0
            for rate in rates:
                if cancel_event and cancel_event.is_set():
                    summary["cancelled"] = True
                    break
                row = dict(baudrate=rate, baudrate_applied=False, rounds_requested=rounds,
                           rounds_passed=0, rounds=[], passed=False, bytes_per_s=0.0, error="")
                summary["results"].append(row)
                notify(dict(event="candidate", baudrate=rate, simulated=not supports))
                try:
                    if supports:
                        client.transport.set_baudrate(rate)
                        row["baudrate_applied"] = True
                        if cancel_event:
                            if cancel_event.wait(settle_s):
                                raise BenchmarkCancelled("scan cancelled")
                        else:
                            time.sleep(settle_s)
                    sync()
                    # Normal control retries are allowed during each test, never for data.
                    client.timeout, client.retries = original_timeout, original_retries
                    for number in range(rounds):
                        outcome = run_benchmark(client, size=size, file_path=file_path, sink=sink,
                                                seed=number + 1, cancel_event=cancel_event)
                        row["rounds"].append(outcome)
                        row["rounds_passed"] += int(outcome["passed"])
                        notify(dict(event="round", baudrate=rate, round=number + 1, **outcome))
                        if not outcome["passed"]:
                            row["error"] = outcome["error"] or "whole-file verification failed"
                            break
                    row["passed"] = row["rounds_passed"] == rounds
                    elapsed = sum(item["elapsed_us"] for item in row["rounds"])
                    row["bytes_per_s"] = sum(item["received_bytes"] for item in row["rounds"]) * 1e6 / max(1, elapsed)
                except BenchmarkCancelled as exc:
                    row["error"] = str(exc)
                    summary["cancelled"] = True
                    break
                except (OSError, ValueError, RuntimeError) as exc:
                    row["error"] = str(exc)
                finally:
                    client.timeout, client.retries = probe_timeout, 0
                notify(dict(event="result", **row))
        except _ProgressError as exc:
            raise exc.original
        finally:
            try:
                if supports:
                    client.transport.set_baudrate(original)
                    time.sleep(settle_s)
                sync()
            except Exception as exc:
                summary.update(restored=False, restore_error=str(exc))
            finally:
                client.timeout, client.retries = original_timeout, original_retries
    valid = [row for row in summary["results"] if row["passed"]]
    if supports and valid:
        summary["highest_passed_baudrate"] = max(row["baudrate"] for row in valid)
        # Prefer the lowest rate within 90% of the best measured throughput.
        best_speed = max(row["bytes_per_s"] for row in valid)
        summary["recommended_baudrate"] = min(row["baudrate"] for row in valid if row["bytes_per_s"] >= best_speed * 0.9)
    if progress:
        progress(dict(event="summary", **summary))
    return summary
