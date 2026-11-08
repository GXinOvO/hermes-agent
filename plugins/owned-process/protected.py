"""Native provider combining the reviewed CAS adapter with owned process handles."""
import posixpath
import logging

from agent.terminal_env_provider import TerminalEnvironmentProvider
from tools.file_protected_fuse import ProtectedFuseEnvironment
from .environment import OwnedProjectEnvironment
from .protected_service import ProtectedService, settings


class ProtectedOwnedEnvironment(OwnedProjectEnvironment, ProtectedFuseEnvironment):
    def __init__(self, config, cwd='/cas', timeout=20):
        self.closed = False
        self.service = None
        self.proc = None
        self.cleanup_complete = False
        try:
            self.service = ProtectedService(config)
            self.state = self.service.state
            ProtectedFuseEnvironment.__init__(self, self.service.active, config['label'],
                generation=self.state.name, container_name=self.service.name)
            self._initialize_transport(self.service.transport, cwd, timeout)
        except BaseException as exc:
            try:
                self.cleanup()
            except Exception as cleanup_error:
                logging.getLogger(__name__).exception('Protected transport rollback failed')
                exc.add_note('protected transport rollback: ' + repr(cleanup_error))
            raise

    def cleanup(self):
        error = None
        if hasattr(self, 'handles'):
            try:
                OwnedProjectEnvironment.cleanup(self)
            except Exception as exc:
                logging.getLogger(__name__).exception('Protected owned transport cleanup failed')
                error = exc
        if self.service is not None:
            self.service.close()
        self.closed = True
        if error is not None:
            raise error

    def execute_file_probe(self, command, stdin_data=None):
        return self.execute(command, stdin_data=stdin_data)


class ProtectedOwnedProvider(TerminalEnvironmentProvider):
    name = 'protected_owned'
    display_name = 'Protected files and owned processes'
    is_remote = False
    is_container = True
    supports_streams = False
    session_isolated_when_nonpersistent = True

    def __init__(self, config=None):
        self.config = config

    @property
    def default_cwd(self):
        return '/cas'

    @property
    def skip_container_guards(self):
        return False

    @property
    def description(self):
        return 'Opt-in private-offline project volume with conditional publication and owned command lifetimes.'

    def is_available(self):
        try:
            settings(self.config)
            return True
        except (OSError, ValueError, TypeError):
            return False

    def check_requirements(self, config):
        settings(self.config)
        return True

    def setup_instructions(self):
        return ['Configure plugins.entries.owned-process.settings.protected with authority_root, state_root, volume, label and pinned image.',
                'Select terminal.backend: protected_owned and cwd: /cas; only private offline networking is qualified.',
                'Existing volume ownership is checked; cleanup retains project data. Export through terminal/read_file before explicit owner-controlled volume retirement.']

    def create_environment(self, *, cwd, timeout, **kwargs):
        settings(self.config)
        if cwd != posixpath.normpath(cwd) or not (cwd == '/cas' or cwd.startswith('/cas/')):
            raise ValueError('protected cwd must be within /cas')
        return ProtectedOwnedEnvironment(self.config, cwd, timeout)
