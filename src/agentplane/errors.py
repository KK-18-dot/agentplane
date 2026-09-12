"""Exit-code contract shared by every command.

0   done
1   provider failed (or the result could not be recorded)
2   usage / configuration error
3   safety boundary violated (bad --dir, forbidden output path, recursion, secret-like env)
4   provider exited 0 but produced (almost) no output
124 timeout
"""

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_SAFETY = 3
EXIT_EMPTY = 4
EXIT_TIMEOUT = 124


class AgentplaneError(Exception):
    """Base error; ``code`` is the process exit code."""

    code = EXIT_FAILED

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        if code is not None:
            self.code = code


class ConfigError(AgentplaneError):
    code = EXIT_USAGE


class UsageError(AgentplaneError):
    code = EXIT_USAGE


class SafetyError(AgentplaneError):
    code = EXIT_SAFETY
