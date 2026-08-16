class StressStackError(Exception):
    """Base exception for expected, user-facing failures."""


class InputError(StressStackError):
    """Raised when an input is neither a supported URL nor a Git repository."""


class GitError(StressStackError):
    """Raised when a mandatory Git operation fails."""


class MetadataError(StressStackError):
    """Raised when generated metadata cannot be validated or written."""


class ToolingError(StressStackError):
    """Raised when a required external tool cannot be provisioned or run."""
