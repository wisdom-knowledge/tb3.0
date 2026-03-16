Background:

You are a DevOps engineer configuring a sidecar proxy container. The container
runs with NET_ADMIN capability, which is standard for sidecar proxy init
containers (e.g., Envoy, Linkerd-proxy). Your job is to configure transparent
traffic interception using iptables so that outbound HTTP requests from a
specific user are routed through a local proxy without modifying the application
binary or its configuration.

The appuser process makes outbound HTTP requests to various internal services.
For compliance auditing, all such requests must be transparently routed through
a local sidecar proxy at 127.0.0.1:8080.


Task:

Write a bash script named setup_proxy.sh in the current directory that
configures iptables rules in the nat table OUTPUT chain to implement transparent
sidecar proxy interception.

The script will be executed with sudo. The rules must satisfy the following
conditions:

1. Outbound HTTP traffic (port 80) from appuser must be redirected to the local
   proxy at 127.0.0.1:8080. You can match traffic by the UID of the process
   that generated it using the iptables owner module, and redirect it using the
   REDIRECT target.

2. Traffic destined to 127.0.0.3 (simulated metadata service) must not be
   redirected. It must reach its original destination on port 80 unchanged.

3. Traffic from proxyuser or any other user must not be redirected, to avoid
   proxy loops and unintended interception.

Note that the order of rules matters. Exception rules should be placed before
the redirect rule so that matching packets are returned early without being
intercepted.
