#!/usr/bin/env python3
"""scripts/agent_tester_mcp.py - MCP Server for sandboxed agent test execution.

This server acts as a privileged bridge on the host, allowing sandboxed
agents to execute tests without exposing /var/run/docker.sock.
"""

from pathlib import Path
import subprocess

# Support both MCP 2.x (MCPServer) and MCP 1.x (FastMCP)
try:
    from mcp.server.mcpserver import MCPServer

    mcp = MCPServer("rush-test-runner")
except ImportError:
    from mcp.server.fastmcp import FastMCP  # type: ignore[no-redef]

    mcp = FastMCP("rush-test-runner")  # type: ignore[no-redef]

# Resolve project paths
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "agent-test.sh"


def _execute(cmd: list[str]) -> str:
    """Execute test runner command and return combined output."""
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        output = proc.stdout
        if proc.stderr:
            output += "\n--- STDERR ---\n" + proc.stderr
        clean_output = output.strip()
        if clean_output:
            return clean_output
        return f"Completed with exit code {proc.returncode} (no output)."
    except Exception as exc:
        return f"Execution error: {exc}"


@mcp.tool()
def run_test_function(file: str, function: str) -> str:
    """Run a specific test function in Docker (e.g. 'test_drops.py' 'func_name')."""
    return _execute([str(SCRIPT_PATH), "func", file, function])


@mcp.tool()
def run_test_file(file: str) -> str:
    """Run all tests in a specific test file (e.g. 'test_drops.py')."""
    return _execute([str(SCRIPT_PATH), "file", file])


@mcp.tool()
def run_all_tests() -> str:
    """Run the entire test suite in Docker."""
    return _execute([str(SCRIPT_PATH), "all"])


@mcp.tool()
def clean_test_environment() -> str:
    """Stop and remove test containers and clean tmpfs volumes."""
    return _execute([str(SCRIPT_PATH), "clean"])


if __name__ == "__main__":
    mcp.run()
