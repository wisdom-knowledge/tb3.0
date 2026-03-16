import subprocess
import time
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading


class ProxyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"PROXY_HIT")

    def log_message(self, format, *args):
        pass


class ExternalHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"EXTERNAL_HIT")

    def log_message(self, format, *args):
        pass


def run_server(handler_class, ip, port):
    server = HTTPServer((ip, port), handler_class)
    thread = threading.Thread(target=server.serve_forever)
    thread.daemon = True
    thread.start()
    return server


def setup_module(module):
    # Proxy listens on 127.0.0.1:8080
    run_server(ProxyHandler, "0.0.0.0", 8080)
    # External service listens on port 80 and 8081
    run_server(ExternalHandler, "0.0.0.0", 80)
    run_server(ExternalHandler, "0.0.0.0", 8081)
    time.sleep(1)


def test_appuser_redirected():
    """Traffic from appuser to 127.0.0.2:80 must be intercepted by the proxy."""
    cmd = "sudo -u appuser curl -s -m 2 http://127.0.0.2:80"
    try:
        output = subprocess.check_output(cmd, shell=True, timeout=3).decode("utf-8")
        assert "PROXY_HIT" in output, (
            f"FAIL: appuser -> 127.0.0.2 was NOT redirected. Got: {output}"
        )
    except Exception as e:
        raise AssertionError(f"FAIL: {e}")


def test_metadata_bypass():
    """Traffic from appuser to 127.0.0.3:80 must bypass the proxy."""
    cmd = "sudo -u appuser curl -s -m 2 http://127.0.0.3:80"
    try:
        output = subprocess.check_output(cmd, shell=True, timeout=3).decode("utf-8")
        assert "EXTERNAL_HIT" in output, (
            f"FAIL: appuser -> 127.0.0.3 was wrongly intercepted. Got: {output}"
        )
    except Exception as e:
        raise AssertionError(f"FAIL: {e}")


def test_proxyuser_bypasses():
    """proxyuser traffic must never be intercepted (avoid proxy loops)."""
    cmd = "sudo -u proxyuser curl -s -m 2 http://127.0.0.2:80"
    try:
        output = subprocess.check_output(cmd, shell=True, timeout=3).decode("utf-8")
        assert "EXTERNAL_HIT" in output, (
            f"FAIL: proxyuser was intercepted (loop risk). Got: {output}"
        )
    except Exception as e:
        raise AssertionError(f"FAIL: {e}")


def test_otheruser_bypasses():
    """Other users' traffic must not be intercepted."""
    cmd = "sudo -u otheruser curl -s -m 2 http://127.0.0.2:80"
    try:
        output = subprocess.check_output(cmd, shell=True, timeout=3).decode("utf-8")
        assert "EXTERNAL_HIT" in output, (
            f"FAIL: otheruser was intercepted. Got: {output}"
        )
    except Exception as e:
        raise AssertionError(f"FAIL: {e}")


def test_appuser_non_port80_not_redirected():
    """appuser traffic to non-port-80 must NOT be redirected (rules must be port-specific)."""
    cmd = "sudo -u appuser curl -s -m 2 http://127.0.0.2:8081"
    try:
        output = subprocess.check_output(cmd, shell=True, timeout=3).decode("utf-8")
        assert "EXTERNAL_HIT" in output, (
            f"FAIL: appuser -> 127.0.0.2:8081 was wrongly intercepted. Got: {output}"
        )
    except Exception as e:
        raise AssertionError(f"FAIL: {e}")


def test_iptables_rules_exist():
    """iptables nat OUTPUT chain must contain REDIRECT and RETURN rules (no bypass hacks)."""
    result = subprocess.run(
        ["sudo", "iptables", "-t", "nat", "-L", "OUTPUT", "-n", "-v"],
        capture_output=True,
        text=True,
    )
    rules = result.stdout
    assert "REDIRECT" in rules, (
        f"FAIL: no REDIRECT rule found in nat OUTPUT chain.\n{rules}"
    )
    assert "RETURN" in rules, (
        f"FAIL: no RETURN rule found in nat OUTPUT chain.\n{rules}"
    )
