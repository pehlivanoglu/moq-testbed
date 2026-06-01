class MoqlabError(Exception):
    pass


class ConfigError(MoqlabError):
    pass


class OrchestratorError(MoqlabError):
    pass


class RunNotFoundError(MoqlabError):
    pass


class RunAlreadyExistsError(MoqlabError):
    pass
