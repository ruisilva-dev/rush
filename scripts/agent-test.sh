#!/usr/bin/env bash
# scripts/agent-test.sh - Fast, isolated Docker Compose test runner for Rush API agents and developers.

set -euo pipefail

# Always run relative to the project root directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$SCRIPT_DIR/docker-compose.test.yml" ]]; then
    PROJECT_ROOT="$SCRIPT_DIR"
else
    PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
fi
cd "$PROJECT_ROOT"

COMPOSE_CMD="docker compose --env-file /dev/null -f docker-compose.test.yml"

# Helper to normalize test file paths (strips leading 'tests/' or './tests/' if provided)
normalize_test_path() {
    local target="${1:-}"
    target="${target#./}"
    target="${target#tests/}"
    echo "tests/${target}"
}

usage() {
    cat << 'EOF'
Usage: ./scripts/agent-test.sh <command> [arguments...]

Commands:
  all                  Run full test suite (with migrations)
  file <file>          Run a specific test file (e.g., test_drops.py or tests/test_drops.py)
  func <file> <func>   Run a specific test function (e.g., test_drops.py test_create_drop_success)
  clean                Stop and remove test containers and volumes
  sh <cmd>             Run an arbitrary command inside the test container

Examples:
  ./scripts/agent-test.sh file test_drops.py
  ./scripts/agent-test.sh func test_drops.py test_create_drop_success
  ./scripts/agent-test.sh all
  ./scripts/agent-test.sh clean
EOF
    exit 1
}

case "${1:-}" in
    "all")
        shift || true
        $COMPOSE_CMD run --rm test sh -c "alembic upgrade head && pytest -q --tb=short $*"
        ;;
    "file")
        if [[ -z "${2:-}" ]]; then
            echo "Error: Missing test file." >&2
            usage
        fi
        FILE_PATH="$(normalize_test_path "$2")"
        shift 2 || true
        $COMPOSE_CMD run --rm test sh -c "alembic upgrade head && pytest \"$FILE_PATH\" -q --tb=short -x $*"
        ;;
    "func")
        if [[ -z "${2:-}" || -z "${3:-}" ]]; then
            echo "Error: Missing test file or test function name." >&2
            usage
        fi
        FILE_PATH="$(normalize_test_path "$2")"
        FUNC_NAME="$3"
        shift 3 || true
        $COMPOSE_CMD run --rm test sh -c "alembic upgrade head && pytest \"$FILE_PATH\" -k \"$FUNC_NAME\" -q --tb=short -x $*"
        ;;
    "clean")
        $COMPOSE_CMD down test test_postgres --volumes --remove-orphans
        ;;
    "sh")
        shift || true
        $COMPOSE_CMD run --rm test sh -c "$*"
        ;;
    *)
        usage
        ;;
esac
