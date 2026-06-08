"""
GenCall - SIP Registration Storm

Simulates mass device registration/de-registration:
  - Register N endpoints simultaneously
  - Maintain registrations with periodic re-REGISTER
  - Simulate device reboot (unregister + re-register)
  - Simulate network failover (all devices re-register at once)
  - Track registration success rates and timing

Use cases:
  - SBC/proxy scalability testing
  - Registration storm recovery testing
  - Session border controller benchmarking
"""

import time
import random
import threading
import logging
from dataclasses import dataclass, field
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger("gencall.scenario.reg_storm")


@dataclass
class Endpoint:
    """A simulated SIP endpoint."""
    username: str
    domain: str
    password: str = ""
    registered: bool = False
    register_time_ms: float = 0.0
    expires: int = 3600
    last_registered: float = 0.0
    failures: int = 0
    contact: str = ""

    def needs_refresh(self, now: float) -> bool:
        """Check if registration needs refreshing (re-REGISTER before expiry)."""
        if not self.registered:
            return False
        # Re-register at 80% of expiry time
        return (now - self.last_registered) > (self.expires * 0.8)


@dataclass
class StormStats:
    """Registration storm statistics."""
    total_attempts: int = 0
    successful: int = 0
    failed: int = 0
    avg_register_time_ms: float = 0.0
    min_register_time_ms: float = float("inf")
    max_register_time_ms: float = 0.0
    storm_duration_sec: float = 0.0
    registrations_per_second: float = 0.0

    def to_dict(self) -> dict:
        return {
            "total_attempts": self.total_attempts,
            "successful": self.successful,
            "failed": self.failed,
            "success_rate_pct": round((self.successful / max(1, self.total_attempts)) * 100, 1),
            "avg_register_time_ms": round(self.avg_register_time_ms, 1),
            "min_register_time_ms": round(self.min_register_time_ms, 1) if self.min_register_time_ms != float("inf") else 0,
            "max_register_time_ms": round(self.max_register_time_ms, 1),
            "storm_duration_sec": round(self.storm_duration_sec, 1),
            "registrations_per_second": round(self.registrations_per_second, 1),
        }

    def report(self) -> str:
        return "\n".join([
            "",
            "=" * 55,
            "  REGISTRATION STORM RESULTS",
            "=" * 55,
            f"  Total attempts:     {self.total_attempts}",
            f"  Successful:         {self.successful}",
            f"  Failed:             {self.failed}",
            f"  Success rate:       {(self.successful / max(1, self.total_attempts)) * 100:.1f}%",
            f"  Avg register time:  {self.avg_register_time_ms:.1f} ms",
            f"  Min register time:  {self.min_register_time_ms:.1f} ms" if self.min_register_time_ms != float("inf") else "  Min register time:  N/A",
            f"  Max register time:  {self.max_register_time_ms:.1f} ms",
            f"  Storm duration:     {self.storm_duration_sec:.1f} s",
            f"  Reg/sec:            {self.registrations_per_second:.1f}",
            "=" * 55,
        ])


# ═══════════════════════════════════════════════════════════════════════════════
#  STORM MODES
# ═══════════════════════════════════════════════════════════════════════════════

def generate_endpoints(base_user: str, domain: str, count: int,
                       password: str = "") -> list[Endpoint]:
    """Generate a list of simulated endpoints."""
    endpoints = []
    for i in range(count):
        ep = Endpoint(
            username=f"{base_user}{i:05d}",
            domain=domain,
            password=password,
            expires=3600,
        )
        endpoints.append(ep)
    return endpoints


def run(ctx):
    """
    Registration storm scenario.

    Config params (in ctx.config):
        storm_mode:     "burst" | "gradual" | "failover" | "churn"
        num_endpoints:  Number of endpoints to simulate
        base_user:      Username prefix (e.g., "phone")
        domain:         SIP domain
        password:       Auth password
        burst_delay_ms: Delay between registrations in gradual mode
    """
    messages = ctx.messages
    parameters = ctx.parameters
    config = ctx.config

    mode = config.get("storm_mode", "burst")
    num_endpoints = int(config.get("num_endpoints", 100))
    base_user = config.get("base_user", "gencall")
    domain = config.get("domain", parameters.get("remote_host", "10.0.0.1"))
    password = config.get("password", "")
    burst_delay = float(config.get("burst_delay_ms", 0)) / 1000.0

    logger.info("Registration storm: mode=%s, endpoints=%d, domain=%s",
                mode, num_endpoints, domain)

    endpoints = generate_endpoints(base_user, domain, num_endpoints, password)
    stats = StormStats()
    start_time = time.time()

    if mode == "burst":
        # All endpoints register simultaneously
        _storm_burst(ctx, endpoints, stats, messages, parameters)

    elif mode == "gradual":
        # Endpoints register one by one with delay
        _storm_gradual(ctx, endpoints, stats, messages, parameters, burst_delay)

    elif mode == "failover":
        # Register all, wait, then simulate network failover (all re-register)
        _storm_burst(ctx, endpoints, stats, messages, parameters)
        logger.info("Simulating network failover in 10s...")
        ctx.sleep(10)
        # Reset all registrations
        for ep in endpoints:
            ep.registered = False
        logger.info("FAILOVER - All endpoints re-registering")
        _storm_burst(ctx, endpoints, stats, messages, parameters)

    elif mode == "churn":
        # Continuous register/unregister churn
        _storm_churn(ctx, endpoints, stats, messages, parameters, duration=60)

    stats.storm_duration_sec = time.time() - start_time
    if stats.storm_duration_sec > 0:
        stats.registrations_per_second = stats.successful / stats.storm_duration_sec

    logger.info(stats.report())
    ctx.quit("Storm complete", str(stats.to_dict()))


def _register_one(ctx, endpoint: Endpoint, messages, parameters) -> float:
    """Register a single endpoint. Returns time in ms, or -1 on failure."""
    params = dict(parameters)
    params["fromNumber"] = endpoint.username
    params["authUser"] = endpoint.username
    params["authPass"] = endpoint.password

    dialog = ctx.new_dialog()
    start = time.time()

    try:
        register = messages.REGISTER(params)
        dialog.send(register)

        reply = register.get_reply(10)
        if not reply:
            return -1

        if reply.get_code() == "401" or reply.get_code() == "407":
            # Auth challenge - re-register with credentials
            register_auth = messages.REGISTER_AUTH(params)
            dialog.send(register_auth)
            reply = register_auth.get_reply(10)

        elapsed_ms = (time.time() - start) * 1000

        if reply and reply.get_code() == "200":
            endpoint.registered = True
            endpoint.register_time_ms = elapsed_ms
            endpoint.last_registered = time.time()
            return elapsed_ms
        else:
            endpoint.failures += 1
            return -1

    except Exception as e:
        logger.debug("Register failed for %s: %s", endpoint.username, e)
        endpoint.failures += 1
        return -1


def _storm_burst(ctx, endpoints, stats, messages, parameters):
    """Register all endpoints as fast as possible."""
    times = []

    for ep in endpoints:
        result = _register_one(ctx, ep, messages, parameters)
        stats.total_attempts += 1
        if result >= 0:
            stats.successful += 1
            times.append(result)
            stats.min_register_time_ms = min(stats.min_register_time_ms, result)
            stats.max_register_time_ms = max(stats.max_register_time_ms, result)
        else:
            stats.failed += 1

    if times:
        stats.avg_register_time_ms = sum(times) / len(times)


def _storm_gradual(ctx, endpoints, stats, messages, parameters, delay: float):
    """Register endpoints one by one with a delay between each."""
    times = []

    for ep in endpoints:
        result = _register_one(ctx, ep, messages, parameters)
        stats.total_attempts += 1
        if result >= 0:
            stats.successful += 1
            times.append(result)
            stats.min_register_time_ms = min(stats.min_register_time_ms, result)
            stats.max_register_time_ms = max(stats.max_register_time_ms, result)
        else:
            stats.failed += 1

        if delay > 0:
            ctx.sleep(delay)

    if times:
        stats.avg_register_time_ms = sum(times) / len(times)


def _storm_churn(ctx, endpoints, stats, messages, parameters, duration: float):
    """Continuously register and unregister random endpoints."""
    end_time = time.time() + duration
    times = []

    while time.time() < end_time:
        ep = random.choice(endpoints)

        if ep.registered:
            # Unregister (REGISTER with Expires: 0)
            ep.registered = False
            logger.debug("Unregistered: %s", ep.username)

        result = _register_one(ctx, ep, messages, parameters)
        stats.total_attempts += 1
        if result >= 0:
            stats.successful += 1
            times.append(result)
            stats.min_register_time_ms = min(stats.min_register_time_ms, result)
            stats.max_register_time_ms = max(stats.max_register_time_ms, result)
        else:
            stats.failed += 1

        ctx.sleep(random.uniform(0.01, 0.1))

    if times:
        stats.avg_register_time_ms = sum(times) / len(times)
