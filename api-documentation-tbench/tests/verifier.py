#!/usr/bin/env python3
import random
import string
import sys
import time

import requests

base = "http://localhost:8080"
timeout = 5


def rand_name(prefix):
    suffix = "".join(random.choice(string.ascii_lowercase) for _ in range(6))
    return f"{prefix}-{suffix}"


def rand_email():
    return f"{rand_name('user')}@example.com"


for _ in range(30):
    try:
        r = requests.get(f"{base}/api/docs", timeout=1)
        if r.status_code == 200:
            break
    except Exception:
        pass
    time.sleep(1)
else:
    print("Service not ready")
    sys.exit(1)

r = requests.get(f"{base}/api/docs", timeout=timeout)
assert r.status_code == 200
docs = r.json()

assert str(docs["openapi"]).startswith("3.0")
paths = docs["paths"]
assert "/api/docs" not in paths
assert set(paths.keys()) == {"/api/users", "/api/users/{id}", "/api/users/{id}/posts"}

total_methods = sum(len(methods) for methods in paths.values())
assert total_methods == 4, f"Should have exactly 4 methods, got {total_methods}"

for path, methods in paths.items():
    assert ":" not in path, f"OpenAPI path must use braces, got {path}"
    for method, spec in methods.items():
        assert "responses" in spec
        assert len(spec["responses"]) >= 2

post_spec = paths["/api/users"]["post"]
schema = post_spec["requestBody"]["content"]["application/json"]["schema"]
assert "name" in schema["required"]
assert "email" in schema["required"]
assert schema["properties"]["age"]["minimum"] == 1
assert schema["properties"]["age"]["maximum"] == 150
assert set(schema["properties"]["role"]["enum"]) == {"viewer", "editor", "admin"}
assert schema["properties"]["role"]["default"] == "viewer"

posts_spec = paths["/api/users/{id}/posts"]["get"]
params = {param["name"]: param for param in posts_spec["parameters"]}
assert "id" in params and params["id"]["in"] == "path"
assert "published" in params and params["published"]["in"] == "query"
assert "limit" in params and params["limit"]["in"] == "query"
assert params["limit"]["schema"]["minimum"] == 1
assert params["limit"]["schema"]["maximum"] == 50

# Runtime validation
r = requests.post(f"{base}/api/users", json={"age": 25}, timeout=timeout)
assert r.status_code == 400

r = requests.post(f"{base}/api/users", json={"name": "Alice"}, timeout=timeout)
assert r.status_code == 400

r = requests.post(
    f"{base}/api/users",
    json={"name": "Alice", "email": "not-an-email", "age": 25},
    timeout=timeout,
)
assert r.status_code == 400

r = requests.post(
    f"{base}/api/users",
    json={"name": "Alice", "email": rand_email(), "age": 0},
    timeout=timeout,
)
assert r.status_code == 400

r = requests.post(
    f"{base}/api/users",
    json={"name": "Alice", "email": rand_email(), "age": 151},
    timeout=timeout,
)
assert r.status_code == 400

r = requests.post(
    f"{base}/api/users",
    json={"name": "Alice", "email": rand_email(), "role": "superadmin"},
    timeout=timeout,
)
assert r.status_code == 400

user_name = rand_name("alice")
user_email = rand_email()
r = requests.post(
    f"{base}/api/users",
    json={"name": user_name, "email": user_email, "age": 30, "role": "editor"},
    timeout=timeout,
)
assert r.status_code == 201
data = r.json()
assert data["name"] == user_name
assert data["email"] == user_email
assert data["age"] == 30
assert data["role"] == "editor"
user_id = data["id"]

default_user_name = rand_name("default")
default_user_email = rand_email()
r = requests.post(
    f"{base}/api/users",
    json={"name": default_user_name, "email": default_user_email},
    timeout=timeout,
)
assert r.status_code == 201
default_user = r.json()
assert default_user["role"] == "viewer"

r = requests.get(f"{base}/api/users/nonexistent", timeout=timeout)
assert r.status_code == 404

r = requests.get(f"{base}/api/users/{user_id}", timeout=timeout)
assert r.status_code == 200
data = r.json()
assert data["id"] == user_id
assert data["name"] == user_name
assert data["email"] == user_email

r = requests.put(f"{base}/api/users/{user_id}", json={"age": 0}, timeout=timeout)
assert r.status_code == 400

r = requests.put(f"{base}/api/users/{user_id}", json={"age": 151}, timeout=timeout)
assert r.status_code == 400

r = requests.put(
    f"{base}/api/users/{user_id}",
    json={"email": "bad-email"},
    timeout=timeout,
)
assert r.status_code == 400

r = requests.put(
    f"{base}/api/users/{user_id}",
    json={"role": "unknown"},
    timeout=timeout,
)
assert r.status_code == 400

new_name = rand_name("updated")
r = requests.put(
    f"{base}/api/users/{user_id}",
    json={"name": new_name, "age": 35, "role": "admin"},
    timeout=timeout,
)
assert r.status_code == 200
data = r.json()
assert data["name"] == new_name
assert data["age"] == 35
assert data["role"] == "admin"

r = requests.put(f"{base}/api/users/nonexistent", json={"name": "ghost"}, timeout=timeout)
assert r.status_code == 404

r = requests.get(f"{base}/api/users/{user_id}/posts", timeout=timeout)
assert r.status_code == 200
posts = r.json()
assert isinstance(posts, list)
for item in posts:
    assert {"id", "title", "published"} <= set(item.keys())

r = requests.get(f"{base}/api/users/{user_id}/posts?published=maybe", timeout=timeout)
assert r.status_code == 400

r = requests.get(f"{base}/api/users/{user_id}/posts?limit=0", timeout=timeout)
assert r.status_code == 400

r = requests.get(f"{base}/api/users/{user_id}/posts?limit=100", timeout=timeout)
assert r.status_code == 400

r = requests.get(f"{base}/api/users/{user_id}/posts?published=true", timeout=timeout)
assert r.status_code == 200
posts = r.json()
assert all(item["published"] is True for item in posts)

r = requests.get(f"{base}/api/users/{user_id}/posts?published=false", timeout=timeout)
assert r.status_code == 200
posts = r.json()
assert all(item["published"] is False for item in posts)

r = requests.get(f"{base}/api/users/{user_id}/posts?limit=1", timeout=timeout)
assert r.status_code == 200
assert len(r.json()) <= 1

r = requests.get(f"{base}/api/users/nonexistent/posts", timeout=timeout)
assert r.status_code == 404

print("All tests passed")
sys.exit(0)
