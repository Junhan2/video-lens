"""The one error type that reaches the user, and the exit codes of spec 5.3."""

EXIT_OK = 0
EXIT_BAD_ARGS = 2
EXIT_INPUT = 3          # input unreadable or no video stream
EXIT_EMPTY_RANGE = 4
EXIT_DEPENDENCY = 5     # missing dependency or helper build failed


class VlError(Exception):
    """Printed as the single stderr line `video-lens: <what failed>. <what to do>` and mapped to an exit code."""

    def __init__(self, code, what, todo=""):
        super().__init__(what)
        self.code = code
        self.what = what.rstrip(".")
        self.todo = todo

    def line(self):
        text = f"video-lens: {self.what}."
        return f"{text} {self.todo}" if self.todo else text
