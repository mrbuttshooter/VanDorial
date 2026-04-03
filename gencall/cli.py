"""
GenCall CLI - Command-line tools for quick SIP testing without the web UI.
"""

import argparse
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

    args = parser.parse_args()

    if args.command == "run":
        cmd_run(args)
    elif args.command == "scenarios":
        cmd_scenarios(args)
    elif args.command == "server":
        cmd_server(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
