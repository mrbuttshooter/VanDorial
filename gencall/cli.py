"""
GenCall CLI - Command-line tools for quick SIP testing without the web UI.
"""

import argparse
import os
import sys
import time
import signal
import uuid
import logging

from gencall.core.config import Config
from gencall.core.log import setup_logging
from gencall.core.sipp_engine import (
    SIPpEngine, SIPpInstance, SIPpMode, SIPpTransport, SIPpState
)
from gencall.scenarios.manager import ScenarioManager


def cmd_run(args):
    """Run a quick SIP test from the command line."""
    config = Config(args.config)
    setup_logging(config)

    engine = SIPpEngine(config)
    scenarios = ScenarioManager()

    scenario_path = scenarios.get_scenario_path(args.scenario)
    if not scenario_path:
        print(f"ERROR: Scenario '{args.scenario}' not found")
        print("Available:", ", ".join(s["name"] for s in scenarios.list_scenarios()))
        sys.exit(1)

    transport_map = {"udp": SIPpTransport.UDP, "tcp": SIPpTransport.TCP, "tls": SIPpTransport.TLS}

    instance = SIPpInstance(
        id=args.name or f"cli-{uuid.uuid4().hex[:6]}",
        scenario_file=scenario_path,
        remote_host=args.target,
        remote_port=args.port,
        local_ip=args.local_ip or "",
        local_port=args.local_port,
        transport=transport_map.get(args.transport, SIPpTransport.UDP),
        call_rate=args.rate,
        max_calls=args.max_calls,
        call_limit=args.limit,
        duration=args.duration,
        auth_user=args.auth_user or "",
        auth_pass=args.auth_pass or "",
    )

    print(f"GenCall - Starting test: {instance.id}")
    print(f"  Scenario:  {args.scenario}")
    print(f"  Target:    {args.target}:{args.port}")
    print(f"  Rate:      {args.rate} cps")
    print(f"  Limit:     {args.limit} concurrent")
    print()

    # Handle Ctrl+C
    def signal_handler(sig, frame):
        print("\nStopping...")
        engine.stop_all()
        sys.exit(0)
    signal.signal(signal.SIGINT, signal_handler)

    if not engine.start_instance(instance):
        print(f"ERROR: {instance.error_message}")
        sys.exit(1)

    print("Test running. Press Ctrl+C to stop.\n")
    print(f"{'Time':>8} {'Total':>8} {'OK':>8} {'Fail':>8} {'Active':>8} {'CPS':>8} {'Success%':>10}")
    print("-" * 70)

    while instance.state == SIPpState.RUNNING:
        time.sleep(2)
        s = instance.stats
        print(f"{s.uptime:>7.0f}s {s.total_calls:>8} {s.successful_calls:>8} "
              f"{s.failed_calls:>8} {s.current_calls:>8} {s.calls_per_second:>8.1f} "
              f"{s.success_rate:>9.1f}%")

    # Final stats
    s = instance.stats
    print()
    print("=" * 70)
    print(f"Test completed: {instance.state.value}")
    print(f"  Total calls:     {s.total_calls}")
    print(f"  Successful:      {s.successful_calls}")
    print(f"  Failed:          {s.failed_calls}")
    print(f"  Success rate:    {s.success_rate:.1f}%")
    print(f"  Duration:        {s.uptime:.1f}s")
    print(f"  Avg CPS:         {s.calls_per_second:.2f}")

    if instance.error_message:
        print(f"  Error:           {instance.error_message}")


def cmd_scenarios(args):
    """List available scenarios."""
    scenarios = ScenarioManager()
    print("Available SIP Scenarios:")
    print("-" * 60)
    for s in scenarios.list_scenarios():
        print(f"  {s['name']:<20} {s['description']}")


def cmd_keys(args):
    """Manage API keys (create / list / revoke)."""
    config = Config(args.config)
    setup_logging(config)

    from gencall.db.models import Database
    from gencall.core.api_gateway import APIKeyManager

    db = Database(config.db_url)
    db.create_tables()
    mgr = APIKeyManager(db=db)

    if args.keys_command == "create":
        raw_key, key = mgr.create_key(args.name, rate_limit=args.rate_limit)
        print("API key created. Save the key now - it will NOT be shown again:")
        print(f"  key_id:  {key.key_id}")
        print(f"  name:    {key.name}")
        print(f"  X-API-Key: {raw_key}")
    elif args.keys_command == "revoke":
        if mgr.revoke_key(args.key_id):
            print(f"Revoked key {args.key_id}")
        else:
            print(f"Key {args.key_id} not found")
            sys.exit(1)
    else:  # list (default)
        keys = mgr.list_keys()
        if not keys:
            print("No API keys. Create one with: gencall keys create --name <name>")
            return
        print(f"{'key_id':<26} {'name':<20} {'enabled':<8} {'requests':<8}")
        print("-" * 64)
        for k in keys:
            print(f"{k['key_id']:<26} {k['name']:<20} "
                  f"{str(k['enabled']):<8} {k['request_count']:<8}")


def cmd_users(args):
    """Manage console login accounts (create / list / delete / passwd).

    Passwords come from --password, else $GENCALL_USER_PASSWORD, else an
    interactive prompt — so the installer can pass one non-interactively without
    it landing in shell history/argv when run by hand."""
    import getpass
    config = Config(args.config)
    setup_logging(config)

    from gencall.db.models import Database
    from gencall.core.auth_users import UserManager

    db = Database(config.db_url)
    db.create_tables()
    mgr = UserManager(db=db)

    def _resolve_password() -> str:
        pw = getattr(args, "password", "") or os.environ.get("GENCALL_USER_PASSWORD", "")
        if pw:
            return pw
        pw = getpass.getpass("Password: ")
        if pw != getpass.getpass("Confirm password: "):
            print("Passwords do not match."); sys.exit(1)
        return pw

    if args.users_command == "create":
        try:
            user = mgr.create_user(args.username, _resolve_password())
        except ValueError as e:
            print(f"Error: {e}"); sys.exit(1)
        print(f"Console user created: {user['username']} (id={user['id']})")
    elif args.users_command == "passwd":
        # Find the user id by username.
        match = [u for u in mgr.list_users() if u["username"] == args.username]
        if not match:
            print(f"User {args.username!r} not found"); sys.exit(1)
        try:
            mgr.set_password(match[0]["id"], _resolve_password())
        except ValueError as e:
            print(f"Error: {e}"); sys.exit(1)
        print(f"Password updated for {args.username}")
    elif args.users_command == "delete":
        match = [u for u in mgr.list_users() if u["username"] == args.username]
        if not match:
            print(f"User {args.username!r} not found"); sys.exit(1)
        mgr.delete_user(match[0]["id"])
        print(f"Deleted user {args.username}")
    else:  # list (default)
        users = mgr.list_users()
        if not users:
            print("No console users. Create one with: gencall users create <username>")
            return
        print(f"{'id':<5} {'username':<24} {'role':<12} {'enabled':<8}")
        print("-" * 52)
        for u in users:
            print(f"{u['id']:<5} {u['username']:<24} {u['role']:<12} {str(u['enabled']):<8}")


def cmd_server(args):
    """Start the GenCall web server."""
    from gencall.main import main as server_main
    sys.argv = ["gencall"]
    if args.config:
        sys.argv.extend(["-c", args.config])
    if args.host:
        sys.argv.extend(["-H", args.host])
    if args.port:
        sys.argv.extend(["-p", str(args.port)])
    server_main()


def main():
    parser = argparse.ArgumentParser(
        prog="gencall",
        description="GenCall - SIP Traffic Generator v2.0",
    )
    sub = parser.add_subparsers(dest="command", help="Command")

    # Run command
    run_parser = sub.add_parser("run", help="Run a quick SIP test")
    run_parser.add_argument("target", help="Remote SIP host (IP or hostname)")
    run_parser.add_argument("-s", "--scenario", default="basic_call", help="Scenario name")
    run_parser.add_argument("-p", "--port", type=int, default=5060, help="Remote SIP port")
    run_parser.add_argument("-r", "--rate", type=float, default=1.0, help="Calls per second")
    run_parser.add_argument("-l", "--limit", type=int, default=10, help="Concurrent call limit")
    run_parser.add_argument("-m", "--max-calls", type=int, default=0, help="Max total calls (0=unlimited)")
    run_parser.add_argument("-d", "--duration", type=int, default=0, help="Test duration seconds (0=forever)")
    run_parser.add_argument("-t", "--transport", default="udp", choices=["udp", "tcp", "tls"])
    run_parser.add_argument("--local-ip", default="", help="Local bind IP")
    run_parser.add_argument("--local-port", type=int, default=5060, help="Local SIP port")
    run_parser.add_argument("--auth-user", default="", help="Auth username")
    run_parser.add_argument("--auth-pass", default="", help="Auth password")
    run_parser.add_argument("-n", "--name", default="", help="Test instance name")
    run_parser.add_argument("-c", "--config", default=None, help="Config file path")

    # Scenarios command
    sc_parser = sub.add_parser("scenarios", help="List available scenarios")

    # Server command
    srv_parser = sub.add_parser("server", help="Start the web server")
    srv_parser.add_argument("-c", "--config", default=None, help="Config file path")
    srv_parser.add_argument("-H", "--host", default=None, help="Bind address")
    srv_parser.add_argument("-p", "--port", type=int, default=None, help="Port")

    # Keys command (API key management)
    keys_parser = sub.add_parser("keys", help="Manage API keys")
    keys_parser.add_argument("-c", "--config", default=None, help="Config file path")
    keys_sub = keys_parser.add_subparsers(dest="keys_command", help="Key action")

    k_create = keys_sub.add_parser("create", help="Create a new API key")
    k_create.add_argument("-n", "--name", default="admin", help="Human-readable key name")
    k_create.add_argument("--rate-limit", type=int, default=60,
                          help="Requests per minute allowed for this key")

    keys_sub.add_parser("list", help="List API keys")

    k_revoke = keys_sub.add_parser("revoke", help="Revoke an API key")
    k_revoke.add_argument("key_id", help="The key_id to revoke")

    # Users command (console login accounts)
    users_parser = sub.add_parser("users", help="Manage console login accounts")
    users_parser.add_argument("-c", "--config", default=None, help="Config file path")
    users_sub = users_parser.add_subparsers(dest="users_command", help="User action")

    u_create = users_sub.add_parser("create", help="Create a console login account")
    u_create.add_argument("username", help="Login username")
    u_create.add_argument("--password", default="",
                          help="Password (else $GENCALL_USER_PASSWORD or prompt)")

    u_passwd = users_sub.add_parser("passwd", help="Reset a user's password")
    u_passwd.add_argument("username", help="Login username")
    u_passwd.add_argument("--password", default="",
                          help="New password (else $GENCALL_USER_PASSWORD or prompt)")

    u_delete = users_sub.add_parser("delete", help="Delete a console account")
    u_delete.add_argument("username", help="Login username")

    users_sub.add_parser("list", help="List console accounts")

    args = parser.parse_args()

    if args.command == "run":
        cmd_run(args)
    elif args.command == "scenarios":
        cmd_scenarios(args)
    elif args.command == "server":
        cmd_server(args)
    elif args.command == "keys":
        cmd_keys(args)
    elif args.command == "users":
        cmd_users(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
