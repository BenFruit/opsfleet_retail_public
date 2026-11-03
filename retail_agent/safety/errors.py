class ResultGuardError(ValueError):
    """A guard withheld data: a sensitive value or a result that cannot be verified.

    The message is fixed and safe to log; it never contains the withheld value.
    """
