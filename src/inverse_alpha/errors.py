class InverseAlphaError(Exception):
    """Base exception for expected, user-facing failures."""


class InputError(InverseAlphaError):
    """Raised when an input is neither a supported URL nor a Git repository."""


class GitError(InverseAlphaError):
    """Raised when a mandatory Git operation fails."""


class MetadataError(InverseAlphaError):
    """Raised when generated metadata cannot be validated or written."""

