"""
GenCall Scenario Manager.
Manages SIPp XML scenarios - built-in templates and custom scenarios.
"""

import os
import logging
import re
from typing import Optional

logger = logging.getLogger("gencall.scenarios")

SCENARIO_DIR = os.path.join(os.path.dirname(__file__), "templates")

# Scenario names map onto "<name>.xml" files, so a name MUST be a bare filename
# token — no path separators, no "..", no leading dot. Without this an API caller
# could read/overwrite/delete arbitrary .xml files (e.g. the loop_uac.xml
# template) via name="../../../path". Names are simple identifiers in practice.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def _is_safe_name(name: str) -> bool:
    """True iff ``name`` is a safe bare scenario identifier (no traversal)."""
    return bool(name) and ".." not in name and _SAFE_NAME.match(name) is not None


# SIPp runs a scenario's <exec command="..."/> / int_cmd as a SHELL command, so a
# saved custom scenario that the test runner later executes is remote code
# execution. Custom scenarios only ever need the media/rtp exec forms
# (rtp_stream, play_pcap_audio), so reject the shell-exec attributes outright.
_DANGEROUS_EXEC = re.compile(r"<exec\b[^>]*\b(command|int_cmd)\s*=", re.IGNORECASE)


def reject_dangerous_scenario(content: str) -> None:
    """Raise ValueError if ``content`` contains a shell-executing SIPp action.

    Blocks ``<exec command=...>`` / ``<exec int_cmd=...>`` (arbitrary command
    execution on the worker). The benign media forms (``rtp_stream``,
    ``play_pcap_audio``) are left alone."""
    if _DANGEROUS_EXEC.search(content or ""):
        raise ValueError(
            "scenario rejected: <exec command=...>/<exec int_cmd=...> is not "
            "allowed (it runs shell commands on the worker)")


class ScenarioManager:
    """Manages SIP test scenarios for SIPp."""

    def __init__(self, custom_dir: str = ""):
        self.custom_dir = custom_dir
        self._builtin = self._load_builtins()

    def _load_builtins(self) -> dict:
        """Load built-in scenario templates."""
        scenarios = {}
        if os.path.isdir(SCENARIO_DIR):
            for f in os.listdir(SCENARIO_DIR):
                if f.endswith(".xml"):
                    name = f.replace(".xml", "")
                    scenarios[name] = os.path.join(SCENARIO_DIR, f)
        return scenarios

    def list_scenarios(self) -> list[dict]:
        """List all available scenarios."""
        result = []
        for name, path in self._builtin.items():
            result.append({
                "name": name,
                "path": path,
                "type": "builtin",
                "description": self._get_description(name),
            })
        if self.custom_dir and os.path.isdir(self.custom_dir):
            for f in os.listdir(self.custom_dir):
                if f.endswith(".xml"):
                    name = f.replace(".xml", "")
                    result.append({
                        "name": name,
                        "path": os.path.join(self.custom_dir, f),
                        "type": "custom",
                        "description": "Custom scenario",
                    })
        return result

    def get_scenario_path(self, name: str) -> Optional[str]:
        """Get the file path for a named scenario."""
        if name in self._builtin:
            return self._builtin[name]
        if not _is_safe_name(name):
            return None
        if self.custom_dir:
            custom_path = os.path.join(self.custom_dir, f"{name}.xml")
            if os.path.exists(custom_path):
                return custom_path
        return None

    def get_scenario_content(self, name: str) -> Optional[str]:
        """Read the XML content of a scenario."""
        path = self.get_scenario_path(name)
        if path and os.path.exists(path):
            with open(path, "r") as f:
                return f.read()
        return None

    def save_custom_scenario(self, name: str, content: str) -> str:
        """Save a custom scenario XML file."""
        if not self.custom_dir:
            raise ValueError("No custom scenario directory configured")
        if not _is_safe_name(name):
            raise ValueError(
                "invalid scenario name (use letters, digits, '_', '-', '.'; "
                "no path separators or '..')")
        reject_dangerous_scenario(content)
        os.makedirs(self.custom_dir, exist_ok=True)
        path = os.path.join(self.custom_dir, f"{name}.xml")
        with open(path, "w") as f:
            f.write(content)
        logger.info("Saved custom scenario: %s", path)
        return path

    def delete_custom_scenario(self, name: str) -> bool:
        """Delete a custom scenario."""
        if not self.custom_dir:
            return False
        if not _is_safe_name(name):
            return False
        path = os.path.join(self.custom_dir, f"{name}.xml")
        if os.path.exists(path):
            os.remove(path)
            return True
        return False

    @staticmethod
    def _get_description(name: str) -> str:
        descriptions = {
            "basic_call": "Basic SIP call (INVITE → 200 OK → ACK → BYE)",
            "basic_register": "SIP REGISTER with authentication",
            "call_with_auth": "SIP call with digest authentication",
            "call_with_rtp": "SIP call with RTP media streaming",
            "uas_answer": "UAS: Answer incoming calls with 200 OK",
            "uas_busy": "UAS: Reject calls with 486 Busy Here",
            "call_transfer": "SIP call with blind transfer (REFER)",
            "options_ping": "SIP OPTIONS keep-alive / ping test",
            "stress_test": "High-rate INVITE flood for stress testing",
            "ivr_dtmf": "IVR test with DTMF digit injection",
        }
        return descriptions.get(name, "SIP scenario")
