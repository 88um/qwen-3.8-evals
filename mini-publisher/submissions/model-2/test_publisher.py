#!/usr/bin/env python3
"""Tests for the mini-publisher."""

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request

SUBMISSION_DIR = os.path.dirname(os.path.abspath(__file__))
PUBLISHER = os.path.join(SUBMISSION_DIR, "publisher")
RECEIVER = os.path.join(SUBMISSION_DIR, os.pardir, os.pardir, "receiver.py")
TEST_STATE = os.path.join(SUBMISSION_DIR, "test_state")
TEST_PORT = 8341
RECEIVER_URL = f"http://127.0.0.1:{TEST_PORT}"


def clean_state():
    if os.path.exists(TEST_STATE):
        shutil.rmtree(TEST_STATE)
    os.makedirs(TEST_STATE, exist_ok=True)


def run_publisher(*args):
    env = {**os.environ, "PUBLISHER_STATE_DIR": TEST_STATE, "RECEIVER_URL": RECEIVER_URL}
    result = subprocess.run(
        [sys.executable, PUBLISHER] + list(args),
        env=env, capture_output=True, text=True, timeout=10,
    )
    return result


def get_status():
    result = run_publisher("status", "--json")
    return json.loads(result.stdout)


def audit(message_id):
    url = f"{RECEIVER_URL}/audit?message_id={message_id}"
    resp = urllib.request.urlopen(url, timeout=5)
    return json.loads(resp.read())["accepted_count"]


def reset_receiver():
    req = urllib.request.Request(f"{RECEIVER_URL}/reset", method="POST")
    urllib.request.urlopen(req, timeout=5)


def start_receiver():
    env = {**os.environ, "PORT": str(TEST_PORT), "SEED": "42"}
    proc = subprocess.Popen(
        [sys.executable, RECEIVER],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    time.sleep(0.5)
    return proc


def start_daemon():
    env = {**os.environ, "PUBLISHER_STATE_DIR": TEST_STATE, "RECEIVER_URL": RECEIVER_URL}
    proc = subprocess.Popen(
        [sys.executable, PUBLISHER, "daemon"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    return proc


def test_enqueue_creates_pending():
    clean_state()
    run_publisher("enqueue", "msg-1", "hello world")
    status = get_status()
    assert len(status["messages"]) == 1
    assert status["messages"][0]["id"] == "msg-1"
    assert status["messages"][0]["state"] == "pending"
    assert status["messages"][0]["attempts"] == 0
    print("PASS: enqueue creates pending message")


def test_enqueue_idempotent():
    clean_state()
    run_publisher("enqueue", "msg-1", "hello")
    run_publisher("enqueue", "msg-1", "different payload")
    status = get_status()
    assert len(status["messages"]) == 1
    print("PASS: enqueue is idempotent")


def test_status_while_empty():
    clean_state()
    status = get_status()
    assert status == {"messages": []}
    print("PASS: status works with no messages")


def test_delivery_and_at_most_once(receiver_proc):
    clean_state()
    reset_receiver()

    for i in range(5):
        run_publisher("enqueue", f"test-{i}", f"payload-{i}")

    daemon = start_daemon()
    try:
        time.sleep(60)
    finally:
        daemon.send_signal(signal.SIGTERM)
        daemon.wait(timeout=5)

    status = get_status()
    for msg in status["messages"]:
        count = audit(msg["id"])
        if msg["state"] == "delivered":
            assert count == 1, f"{msg['id']}: state=delivered but accepted_count={count}"
        elif msg["state"] == "failed":
            assert count == 0, f"{msg['id']}: state=failed but accepted_count={count}"
        assert count <= 1, f"DUPLICATE: {msg['id']} accepted {count} times"

    delivered = sum(1 for m in status["messages"] if m["state"] == "delivered")
    print(f"PASS: {delivered}/5 delivered, at-most-once verified for all")


def test_crash_recovery(receiver_proc):
    clean_state()
    reset_receiver()

    for i in range(3):
        run_publisher("enqueue", f"crash-{i}", f"payload-{i}")

    # Start daemon, kill it after a few seconds, restart
    daemon = start_daemon()
    time.sleep(5)
    daemon.kill()
    daemon.wait()

    status_mid = get_status()
    print(f"  After kill: {[(m['id'], m['state']) for m in status_mid['messages']]}")

    daemon = start_daemon()
    time.sleep(45)
    daemon.send_signal(signal.SIGTERM)
    daemon.wait(timeout=5)

    status = get_status()
    for msg in status["messages"]:
        count = audit(msg["id"])
        assert count <= 1, f"DUPLICATE after crash: {msg['id']} accepted {count} times"

    print("PASS: crash recovery maintains at-most-once")


if __name__ == "__main__":
    test_enqueue_creates_pending()
    test_enqueue_idempotent()
    test_status_while_empty()

    print("\nStarting receiver for integration tests...")
    receiver = start_receiver()
    try:
        test_delivery_and_at_most_once(receiver)
        test_crash_recovery(receiver)
    finally:
        receiver.kill()
        receiver.wait()

    print("\nAll tests passed!")
