#!/bin/bash

cat << 'SETUP' > /workspace/setup_proxy.sh
#!/bin/bash

# Flush existing OUTPUT nat rules to start clean
iptables -t nat -F OUTPUT

# Exception: do not redirect proxyuser traffic (avoid proxy loops)
iptables -t nat -A OUTPUT -m owner --uid-owner proxyuser -j RETURN

# Exception: do not redirect traffic to the metadata service
iptables -t nat -A OUTPUT -d 127.0.0.3 -j RETURN

# Redirect appuser outbound HTTP (port 80) to the local sidecar proxy
iptables -t nat -A OUTPUT -m owner --uid-owner appuser -p tcp --dport 80 -j REDIRECT --to-port 8080
SETUP

chmod +x /workspace/setup_proxy.sh
