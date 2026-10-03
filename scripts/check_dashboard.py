#!/usr/bin/env python3
"""Check the running dashboard end to end: it is up, shut to anyone not signed in, sends its
security headers, refuses a wrong password, lets the right one in, and its three panels have live
data. Standard library only. Prints no secret.

    DASHBOARD_PASSWORD=... python3 scripts/check_dashboard.py [--url http://127.0.0.1:4400]
    DASHBOARD_PASSWORD=... python3 scripts/check_dashboard.py --expect-data

`--expect-data` also requires tool calls and a decided approval in the data (a run of the simulator
leaves both). The password is read from the environment, never an argument.
"""

import argparse
import http.client
import json
import os
import sys
import urllib.parse

COOKIE = "__Host-aig_session"


class CheckError(Exception):
    pass


class Client:
    def __init__(self, base: str) -> None:
        parts = urllib.parse.urlsplit(base)
        self.host = parts.netloc
        self.cookie: str | None = None

    def request(
        self, method: str, path: str, *, body: dict[str, str] | None = None, origin: bool = True
    ) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection(self.host, timeout=20)
        headers = {"Host": self.host}
        payload = None
        if self.cookie:
            headers["Cookie"] = f"{COOKIE}={self.cookie}"
        if body is not None:
            payload = urllib.parse.urlencode(body).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if method == "POST" and origin:
            headers["Origin"] = f"http://{self.host}"
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        data = response.read()
        collected = {key.lower(): value for key, value in response.getheaders()}
        cookies = [value for key, value in response.getheaders() if key.lower() == "set-cookie"]
        for value in cookies:
            if value.startswith(f"{COOKIE}="):
                token = value.split(";", 1)[0].split("=", 1)[1]
                self.cookie = token or None
        connection.close()
        return response.status, collected, data


def check(condition: bool, what: str) -> None:
    if not condition:
        raise CheckError(what)
    print(f"ok    {what}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--url", default="http://127.0.0.1:4400")
    parser.add_argument("--expect-data", action="store_true")
    args = parser.parse_args()
    password = os.environ.get("DASHBOARD_PASSWORD", "")
    if not password:
        sys.exit("check_dashboard: set DASHBOARD_PASSWORD")
    client = Client(args.url)

    status, _, body = client.request("GET", "/healthz")
    check(
        status == 200 and json.loads(body) == {"status": "ok"},
        "the health check answers without a sign-in",
    )
    status, headers, _ = client.request("GET", "/")
    check(
        status == 303 and headers.get("location", "").endswith("/signin"),
        "the page sends a visitor to sign in",
    )
    status, _, _ = client.request("GET", "/api/live")
    check(status == 401, "the data endpoint answers 401 without a session")
    status, headers, _ = client.request("GET", "/signin")
    policy = headers.get("content-security-policy", "")
    check(
        status == 200
        and "default-src 'none'" in policy
        and "'nonce-" in policy
        and "unsafe-inline" not in policy,
        "the sign-in page carries a nonce CSP with nothing inline",
    )
    check(
        headers.get("x-content-type-options") == "nosniff"
        and headers.get("x-frame-options") == "DENY",
        "the security headers are set",
    )

    status, _, _ = client.request("POST", "/api/signin", body={"password": password}, origin=False)
    check(status == 403 and client.cookie is None, "a sign-in with no Origin is refused")
    status, headers, _ = client.request(
        "POST", "/api/signin", body={"password": "not-the-password"}
    )
    check(
        status == 303 and "error=denied" in headers.get("location", "") and client.cookie is None,
        "a wrong password is denied and gives no session",
    )
    status, headers, _ = client.request("POST", "/api/signin", body={"password": password})
    check(status == 303 and client.cookie is not None, "the right password signs in")

    status, _, body = client.request("GET", "/api/live?range=24h")
    check(status == 200, "the live endpoint answers once signed in")
    overview = json.loads(body)
    for panel in ("kpis", "decisions", "approvals"):
        check(overview[panel]["ok"] is True, f"the {panel} panel reads its data")
    kpis = overview["kpis"]["data"]
    print(f"      {kpis['requests']} requests, {kpis['blocked']} blocked, p95 {kpis['p95Ms']} ms")
    decisions = overview["decisions"]["data"]
    approvals = overview["approvals"]["data"]
    if args.expect_data:
        check(
            kpis["requests"] > 0 and len(decisions["decisions"]) > 0, "there are tool calls to show"
        )
        check(
            len(approvals["recent"]) + len(approvals["pending"]) > 0, "there are approvals to show"
        )
        if decisions["nextCursor"]:
            query = urllib.parse.urlencode({"range": "24h", "cursor": decisions["nextCursor"]})
            status, _, body = client.request("GET", f"/api/decisions?{query}")
            older = json.loads(body)["decisions"]
            shown = {d["requestId"] for d in decisions["decisions"]}
            check(
                status == 200 and older and shown.isdisjoint(d["requestId"] for d in older),
                "the next page is older and repeats nothing",
            )
    status, _, _ = client.request(
        "GET", "/api/decisions?range=24h&cursor=%27%3B%20drop%20table%20x"
    )
    check(status == 200, "a cursor the dashboard did not make is ignored")

    status, headers, _ = client.request("POST", "/api/signout", body={})
    check(status == 303 and client.cookie is None, "signing out clears the session")
    status, _, _ = client.request("GET", "/api/live")
    check(status == 401, "the data endpoint is shut again")
    print("PASS  the dashboard")


if __name__ == "__main__":
    try:
        main()
    except CheckError as failure:
        sys.exit(f"FAIL  {failure}")
