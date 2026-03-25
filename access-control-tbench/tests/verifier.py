#!/usr/bin/env python3
import os
import random
import string
import sys
import time

import requests

base = os.environ.get("BASE_URL", "http://gateway:8080")
spicedb_base = os.environ.get("SPICEDB_BASE_URL", "http://spicedb:8443")
timeout = 5
spicedb_headers = {"Authorization": "Bearer devtoken"}


def rand_name(prefix):
    suffix = "".join(random.choice(string.ascii_lowercase) for _ in range(6))
    return f"{prefix}-{suffix}"


def wait_ready():
    for _ in range(45):
        try:
            r = requests.get(f"{base}/api/users/admin/permissions", timeout=1)
            if r.status_code == 200:
                return
        except Exception:
            pass
        time.sleep(1)
    print("Service not ready")
    sys.exit(1)


def wait_spicedb_ready():
    for _ in range(45):
        try:
            r = requests.post(
                f"{spicedb_base}/v1/schema/read",
                json={},
                headers=spicedb_headers,
                timeout=1,
            )
            if r.status_code == 200:
                return
        except Exception:
            pass
        time.sleep(1)
    print("SpiceDB not ready or schema missing")
    sys.exit(1)


def restart_service():
    try:
        r = requests.post(
            f"{base}/internal/ops/restart",
            headers={"X-Harbor-Op": "restart"},
            timeout=1,
        )
        assert r.status_code in (200, 202, 204), (r.status_code, r.text)
    except requests.RequestException:
        pass

    time.sleep(2)
    wait_ready()
    wait_spicedb_ready()


def spicedb_post(path, payload):
    r = requests.post(
        f"{spicedb_base}{path}",
        json=payload,
        headers=spicedb_headers,
        timeout=timeout,
    )
    assert r.status_code == 200, (path, r.status_code, r.text)
    return r.json()


def read_schema_text():
    payload = spicedb_post("/v1/schema/read", {})
    return payload["schemaText"]


def check_spicedb_permission(resource_type, resource_id, permission, subject_type, subject_id):
    payload = spicedb_post(
        "/v1/permissions/check",
        {
            "resource": {"objectType": resource_type, "objectId": resource_id},
            "permission": permission,
            "subject": {"object": {"objectType": subject_type, "objectId": subject_id}},
            "consistency": {"fullyConsistent": True},
        },
    )
    return payload["permissionship"] == "PERMISSIONSHIP_HAS_PERMISSION"


def user_policy_object(user_id, permission):
    return f"user={user_id}|{encode_permission_object(permission)}"


def role_policy_object(role_name, permission):
    return f"role={role_name}|{encode_permission_object(permission)}"


def encode_permission_object(permission):
    deny = permission.startswith("!")
    raw = permission[1:] if deny else permission
    resource, action = raw.split(":", 1)
    prefix = "deny=" if deny else "allow="
    resource_part = "wildcard" if resource == "*" else resource
    action_part = "wildcard" if action == "*" else action
    return f"{prefix}{resource_part}={action_part}"


def check_allowed(user_id, resource, action, expected):
    r = requests.post(
        f"{base}/api/check-permission",
        json={"userId": user_id, "resource": resource, "action": action},
        timeout=timeout,
    )
    assert r.status_code == 200, (r.status_code, r.text)
    assert r.json()["allowed"] is expected, r.json()


def check_explained(user_id, resource, action, expected_allowed, expected_rule, expected_source):
    r = requests.post(
        f"{base}/api/check-permission",
        json={
            "userId": user_id,
            "resource": resource,
            "action": action,
            "explain": True,
        },
        timeout=timeout,
    )
    assert r.status_code == 200, (r.status_code, r.text)
    payload = r.json()
    assert payload["allowed"] is expected_allowed, payload
    assert payload["matchedRule"] == expected_rule, payload
    assert payload["source"] == expected_source, payload
    return payload


def get_permissions(user_id):
    r = requests.get(f"{base}/api/users/{user_id}/permissions", timeout=timeout)
    assert r.status_code == 200, (r.status_code, r.text)
    perms = r.json()["permissions"]
    assert perms == sorted(set(perms)), perms
    return perms


def get_permission_details(user_id):
    r = requests.get(f"{base}/api/users/{user_id}/permissions?details=true", timeout=timeout)
    assert r.status_code == 200, (r.status_code, r.text)
    payload = r.json()
    details = payload["permissions"]
    perms = [item["permission"] for item in details]
    assert perms == sorted(perms), details
    for item in details:
        assert item["sources"] == sorted(set(item["sources"])), item
    return {item["permission"]: item["sources"] for item in details}


def grant_role(role, permission):
    r = requests.post(
        f"{base}/api/roles/{role}/grant",
        json={"permission": permission},
        timeout=timeout,
    )
    assert r.status_code == 200 and r.json()["success"] is True, (r.status_code, r.text)


def grant_user(user_id, permission):
    r = requests.post(
        f"{base}/api/users/{user_id}/grant",
        json={"permission": permission},
        timeout=timeout,
    )
    assert r.status_code == 200 and r.json()["success"] is True, (r.status_code, r.text)


def create_role(role, permissions, inherits):
    r = requests.post(
        f"{base}/api/roles",
        json={"role": role, "permissions": permissions, "inherits": inherits},
        timeout=timeout,
    )
    assert r.status_code == 200 and r.json()["success"] is True, (r.status_code, r.text)


def bind_role(user_id, role):
    r = requests.post(
        f"{base}/api/users/{user_id}/roles",
        json={"role": role},
        timeout=timeout,
    )
    assert r.status_code == 200 and r.json()["success"] is True, (r.status_code, r.text)


def inherit_role(role, parent):
    r = requests.post(
        f"{base}/api/roles/{role}/inherit",
        json={"parent": parent},
        timeout=timeout,
    )
    assert r.status_code == 200 and r.json()["success"] is True, (r.status_code, r.text)


def expect_non_200(method, path, payload, headers=None):
    r = requests.request(method, f"{base}{path}", json=payload, headers=headers, timeout=timeout)
    assert r.status_code >= 400, (method, path, payload, r.status_code, r.text)


wait_ready()
wait_spicedb_ready()

schema_text = read_schema_text()
assert "definition user {}" in schema_text, schema_text
assert "relation child: role" in schema_text, schema_text
assert "permission assignee = member + child->assignee" in schema_text, schema_text
assert "definition policy" in schema_text, schema_text

# Seeded behavior via gateway
check_allowed("admin", "users", "delete", True)
check_allowed("editor1", "posts", "read", True)
check_allowed("editor1", "posts", "write", True)
check_allowed("viewer1", "posts", "write", False)

# Seeded SpiceDB relationships must exist
assert check_spicedb_permission("role", "admin", "assignee", "user", "admin") is True
assert check_spicedb_permission("role", "editor", "assignee", "user", "editor1") is True
assert check_spicedb_permission("role", "viewer", "assignee", "user", "viewer1") is True
assert check_spicedb_permission("role", "viewer", "assignee", "user", "editor1") is True
assert check_spicedb_permission("policy", role_policy_object("editor", "posts:write"), "allow", "user", "editor1") is True
assert check_spicedb_permission("policy", role_policy_object("viewer", "posts:read"), "allow", "user", "viewer1") is True

# Role/user precedence and mirrored SpiceDB policy objects
grant_role("editor", "!posts:write")
check_explained("editor1", "posts", "write", False, "!posts:write", "role:editor")
assert check_spicedb_permission("policy", role_policy_object("editor", "!posts:write"), "deny", "user", "editor1") is True

grant_user("editor1", "posts:write")
check_explained("editor1", "posts", "write", True, "posts:write", "user:editor1")
assert check_spicedb_permission("policy", user_policy_object("editor1", "posts:write"), "allow", "user", "editor1") is True

# Resource specificity must outrank action specificity
priority_role = rand_name("priority")
create_role(priority_role, ["reports:*", "!*:read", "!reports:archive", "*:archive"], [])
bind_role("viewer1", priority_role)
check_explained("viewer1", "reports", "read", True, "reports:*", f"role:{priority_role}")
check_explained("viewer1", "reports", "archive", False, "!reports:archive", f"role:{priority_role}")
assert check_spicedb_permission("role", priority_role, "assignee", "user", "viewer1") is True
assert check_spicedb_permission("policy", role_policy_object(priority_role, "reports:*"), "allow", "user", "viewer1") is True
assert check_spicedb_permission("policy", role_policy_object(priority_role, "!reports:archive"), "deny", "user", "viewer1") is True

# Dynamic roles and inheritance
auditor_role = rand_name("auditor")
ops_role = rand_name("ops")
leaf_role = rand_name("leaf")
support_role = rand_name("support")

create_role(auditor_role, ["logs:read", "!logs:delete"], ["viewer"])
bind_role("viewer1", auditor_role)
check_explained("viewer1", "logs", "delete", False, "!logs:delete", f"role:{auditor_role}")
assert check_spicedb_permission("policy", role_policy_object(auditor_role, "logs:read"), "allow", "user", "viewer1") is True
assert check_spicedb_permission("policy", role_policy_object(auditor_role, "!logs:delete"), "deny", "user", "viewer1") is True

create_role(ops_role, ["ops:restart", "!ops:delete"], [])
inherit_role(ops_role, auditor_role)
bind_role("multiuser", ops_role)
check_explained("multiuser", "logs", "delete", False, "!logs:delete", f"role:{auditor_role}")
assert check_spicedb_permission("role", auditor_role, "assignee", "user", "multiuser") is True
assert check_spicedb_permission("policy", role_policy_object(ops_role, "ops:restart"), "allow", "user", "multiuser") is True

grant_user("multiuser", "logs:delete")
check_explained("multiuser", "logs", "delete", True, "logs:delete", "user:multiuser")
assert check_spicedb_permission("policy", user_policy_object("multiuser", "logs:delete"), "allow", "user", "multiuser") is True

create_role(leaf_role, ["reports:export"], [ops_role])
bind_role("viewer1", leaf_role)
check_explained("viewer1", "ops", "restart", True, "ops:restart", f"role:{ops_role}")
check_explained("viewer1", "reports", "export", True, "reports:export", f"role:{leaf_role}")
assert check_spicedb_permission("role", ops_role, "assignee", "user", "viewer1") is True
assert check_spicedb_permission("policy", role_policy_object(leaf_role, "reports:export"), "allow", "user", "viewer1") is True

# Deep cycle detection must be recursive
expect_non_200("POST", f"/api/roles/{auditor_role}/inherit", {"parent": leaf_role})

viewer1_before = get_permissions("viewer1")
multiuser_before = get_permissions("multiuser")
viewer1_details_before = get_permission_details("viewer1")
multiuser_details_before = get_permission_details("multiuser")

# Restart recovery
restart_service()

schema_text_after = read_schema_text()
assert "definition policy" in schema_text_after, schema_text_after

check_explained("editor1", "posts", "write", True, "posts:write", "user:editor1")
check_explained("viewer1", "reports", "archive", False, "!reports:archive", f"role:{priority_role}")
check_explained("multiuser", "logs", "delete", True, "logs:delete", "user:multiuser")
assert check_spicedb_permission("policy", user_policy_object("editor1", "posts:write"), "allow", "user", "editor1") is True
assert check_spicedb_permission("policy", role_policy_object(priority_role, "!reports:archive"), "deny", "user", "viewer1") is True
assert check_spicedb_permission("policy", user_policy_object("multiuser", "logs:delete"), "allow", "user", "multiuser") is True

viewer1_after = get_permissions("viewer1")
multiuser_after = get_permissions("multiuser")
viewer1_details_after = get_permission_details("viewer1")
multiuser_details_after = get_permission_details("multiuser")

assert viewer1_after == viewer1_before, (viewer1_before, viewer1_after)
assert multiuser_after == multiuser_before, (multiuser_before, multiuser_after)
assert viewer1_details_after == viewer1_details_before, (viewer1_details_before, viewer1_details_after)
assert multiuser_details_after == multiuser_details_before, (multiuser_details_before, multiuser_details_after)

# New writes after restart must still sync to SpiceDB
grant_role("viewer", "users:*")
create_role(support_role, ["tickets:*", "!tickets:delete"], [])
bind_role("multiuser", support_role)
check_explained("viewer1", "users", "update", True, "users:*", "role:viewer")
check_explained("multiuser", "tickets", "delete", False, "!tickets:delete", f"role:{support_role}")
assert check_spicedb_permission("policy", role_policy_object("viewer", "users:*"), "allow", "user", "viewer1") is True
assert check_spicedb_permission("policy", role_policy_object(support_role, "tickets:*"), "allow", "user", "multiuser") is True
assert check_spicedb_permission("policy", role_policy_object(support_role, "!tickets:delete"), "deny", "user", "multiuser") is True

expect_non_200("POST", "/internal/ops/restart", {}, headers={})
expect_non_200("POST", "/api/users/missing-user/grant", {"permission": "!posts:read"})
expect_non_200("POST", "/api/roles/editor/grant", {"permission": "badformat"})
expect_non_200("POST", "/api/check-permission", {"userId": "viewer1", "resource": "posts"})

check_allowed("admin", rand_name("resource"), rand_name("action"), True)
check_explained("viewer1", rand_name("resource"), rand_name("action"), False, None, None)

print("All tests passed")
sys.exit(0)
