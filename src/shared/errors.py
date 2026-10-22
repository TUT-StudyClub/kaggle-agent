class AgentError(Exception):
    """本文・トークンを含まない公開可能なエラー。"""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


class UncertainWrite(AgentError):
    def __init__(self):
        super().__init__(
            "uncertain_write", "書き込み結果が未確定です。再実行せず照合してください。"
        )
