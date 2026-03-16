import subprocess
import time
import re
import pytest


def run_cmd(cmd, timeout=15):
    """Helper to run a shell command and return result."""
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, shell=isinstance(cmd, str)
    )


def get_qemu_cmdline():
    """Get the full command line of the running qemu-system process."""
    # First, get the PID of the qemu-system process
    result = run_cmd(["pgrep", "-f", "qemu-system"])
    if result.returncode != 0:
        return None
    pid = result.stdout.strip().splitlines()[0].strip()

    # Read full cmdline from /proc - this works even when pgrep -a truncates
    try:
        with open(f"/proc/{pid}/cmdline", "r") as f:
            # cmdline uses null bytes as separators
            cmdline = f.read().replace("\x00", " ").strip()
            if cmdline:
                return cmdline
    except (FileNotFoundError, PermissionError):
        pass

    # Fallback: try ps with wide output
    result = run_cmd(["ps", "-p", pid, "-o", "args=", "--cols", "4096"])
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()

    # Last fallback: pgrep -a (may be truncated in some environments)
    result = run_cmd(["pgrep", "-a", "-f", "qemu-system"])
    if result.returncode == 0:
        for line in result.stdout.strip().splitlines():
            if "qemu-system" in line:
                return line
    return None


class TestQemuRunning:
    """Verify QEMU is properly started with the correct configuration."""

    def test_qemu_process_exists(self):
        """QEMU process must be running."""
        result = run_cmd(["pgrep", "-f", "qemu-system"])
        assert result.returncode == 0, "QEMU process is not running"

    def test_qemu_uses_alpine_iso(self):
        """QEMU must be started with /app/alpine.iso as the boot media."""
        cmdline = get_qemu_cmdline()
        assert cmdline is not None, "QEMU process not found"
        assert "/app/alpine.iso" in cmdline, (
            f"QEMU was not started with /app/alpine.iso. "
            f"Actual command line: {cmdline}"
        )

    def test_qemu_has_port_forwarding(self):
        """QEMU must have user-mode networking with port forwarding to 8080."""
        cmdline = get_qemu_cmdline()
        assert cmdline is not None, "QEMU process not found"
        # Check that hostfwd with port 8080 is configured
        assert "hostfwd" in cmdline and "8080" in cmdline, (
            f"QEMU does not have port forwarding configured for port 8080. "
            f"Actual command line: {cmdline}"
        )


class TestServiceOrigin:
    """Verify the HTTP service truly runs inside the VM, not the container."""

    def test_marker_file_not_in_container(self):
        """The marker file must NOT exist in the container filesystem."""
        result = run_cmd(["test", "-f", "/tmp/vm-marker.txt"])
        assert result.returncode != 0, (
            "FAIL: /tmp/vm-marker.txt exists in the container filesystem. "
            "The file must ONLY exist inside the VM."
        )

    def test_no_container_process_on_8080(self):
        """
        Port 8080 must be listened on by QEMU (user-mode networking),
        not by a separate process like socat, nc, python, etc.
        This prevents bypassing by running a fake HTTP server in the container.
        Uses a whitelist approach: the listener MUST be qemu-system.
        """
        # Get all processes listening on port 8080
        result = run_cmd("ss -tlnp sport = :8080")
        assert result.returncode == 0, "Failed to run ss command"

        listeners = result.stdout.strip()
        if listeners:
            lines = listeners.splitlines()
            for line in lines[1:]:  # skip header
                lower_line = line.lower()
                # The listener process MUST be qemu-system
                # ss -p output format: users:(("qemu-system-x86",pid=xxx,fd=xx))
                assert "qemu-system" in lower_line or "qemu" in lower_line, (
                    f"A non-QEMU process is listening on port 8080. "
                    f"The HTTP service must run inside the VM via QEMU port forwarding, "
                    f"not from a container-level process. Listener info: {line}"
                )

    def test_http_service_responds(self):
        """
        The HTTP service must respond with 'VM-RUNNING' content
        via QEMU port forwarding on port 8080.
        """
        max_retries = 10
        retry_delay = 3

        for i in range(max_retries):
            result = run_cmd(
                ["curl", "-s", "-f", "--max-time", "5",
                 "http://localhost:8080/vm-marker.txt"],
                timeout=10
            )
            if result.returncode == 0:
                content = result.stdout.strip()
                assert "VM-RUNNING" in content, (
                    f"Marker file content incorrect. "
                    f"Expected 'VM-RUNNING', got: {content}"
                )
                return

            if i < max_retries - 1:
                time.sleep(retry_delay)

        pytest.fail(
            f"HTTP service not accessible after {max_retries} retries. "
            "The VM must serve /tmp/vm-marker.txt via HTTP on port 8080."
        )

    def test_vm_marker_via_qemu_console(self):
        """
        Verify /tmp/vm-marker.txt actually exists inside the VM
        by sending a command through QEMU's telnet serial console.
        This is the strongest anti-bypass check.
        """
        cmdline = get_qemu_cmdline()
        assert cmdline is not None, "QEMU process not found"

        # Extract telnet port from QEMU command line
        # Matches patterns like: telnet:127.0.0.1:4444 or telnet:0.0.0.0:4444
        telnet_match = re.search(
            r'telnet:[\w.]+:(\d+)', cmdline or ""
        )
        assert telnet_match is not None, (
            "QEMU must be started with a telnet serial console "
            "(e.g., -serial telnet:127.0.0.1:<port>,server,nowait). "
            f"Actual command line: {cmdline}"
        )

        telnet_port = telnet_match.group(1)
        # Use expect to send a command via telnet and check output.
        # After connecting, send an initial Enter to trigger a fresh prompt,
        # since a previous session may have left the console in an idle state.
        expect_script = f'''
set timeout 30
spawn telnet 127.0.0.1 {telnet_port}
expect {{
    "Escape character" {{ }}
    timeout {{ puts "CONNECT_TIMEOUT"; exit 1 }}
}}
sleep 2
send "\\r"
expect {{
    "#" {{ }}
    "login:" {{
        send "root\\r"
        expect "#"
    }}
    timeout {{ puts "PROMPT_TIMEOUT"; exit 1 }}
}}
send "cat /tmp/vm-marker.txt\\r"
expect {{
    "VM-RUNNING" {{ puts "MARKER_FOUND"; exit 0 }}
    timeout {{ puts "MARKER_NOT_FOUND"; exit 1 }}
}}
'''
        result = run_cmd(
            ["expect", "-c", expect_script],
            timeout=45
        )
        assert result.returncode == 0 and "MARKER_FOUND" in result.stdout, (
            "Failed to verify /tmp/vm-marker.txt inside the VM via "
            f"QEMU serial console on port {telnet_port}. "
            "The file must exist inside the VM with content 'VM-RUNNING'. "
            f"stdout: {result.stdout}, stderr: {result.stderr}"
        )


class TestVmPersistence:
    """Verify the VM stays running in the background."""

    def test_qemu_still_running_after_delay(self):
        """
        After a short wait, QEMU must still be running.
        This ensures the VM is kept in the background as required.
        """
        # First check - QEMU should be running now
        result1 = run_cmd(["pgrep", "-f", "qemu-system"])
        assert result1.returncode == 0, "QEMU is not running at start of persistence check"

        # Wait and check again
        time.sleep(10)

        result2 = run_cmd(["pgrep", "-f", "qemu-system"])
        assert result2.returncode == 0, (
            "QEMU process died after 10 seconds. "
            "The VM must keep running in the background."
        )

    def test_http_service_still_available_after_delay(self):
        """HTTP service must still respond after a delay."""
        time.sleep(5)
        result = run_cmd(
            ["curl", "-s", "-f", "--max-time", "5",
             "http://localhost:8080/vm-marker.txt"],
            timeout=10
        )
        assert result.returncode == 0, (
            "HTTP service stopped responding after delay. "
            "The VM and its HTTP service must remain running."
        )
        assert "VM-RUNNING" in result.stdout, (
            f"HTTP response content changed. Expected 'VM-RUNNING', "
            f"got: {result.stdout.strip()}"
        )
