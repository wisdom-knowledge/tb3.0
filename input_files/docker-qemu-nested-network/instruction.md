In the Docker container environment, start a QEMU virtual machine running Alpine Linux. Inside the VM, create a marker file and start an HTTP server that serves this file. The file must be accessible from outside the container, but must NOT exist in the container's filesystem.

Requirements:
- Start QEMU with the Alpine ISO image located at /app/alpine.iso
- Configure QEMU with a serial console accessible via telnet (e.g., `-serial telnet:127.0.0.1:<port>,server,nowait`)
- Boot into the VM and access it via the serial console
- Inside the VM, create a file at `/tmp/vm-marker.txt` containing the text "VM-RUNNING"
- Start an HTTP server inside the VM that serves this file
- Configure QEMU user-mode networking with port forwarding so the file is accessible via `curl http://localhost:8080/vm-marker.txt`
- The file `/tmp/vm-marker.txt` must NOT exist in the container's filesystem - it should only exist inside the VM
- Keep the VM running in the background

This task verifies that you can properly configure nested virtualization and networking, ensuring the HTTP service truly runs inside the VM rather than in the container.
