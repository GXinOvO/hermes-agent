"""Native opt-in owned process terminal backend."""
import shutil
import sys
import uuid

from agent.terminal_env_provider import TerminalEnvironmentProvider
from .environment import OwnedProjectEnvironment, canonical_directory, protect_runtime


class OwnedProcessProvider(TerminalEnvironmentProvider):
    name = 'owned_process'
    display_name = 'Owned project processes'
    is_remote = False
    is_container = True
    supports_streams = False
    session_isolated_when_nonpersistent = True

    def __init__(self, project_root=None, state_root=None, network='private'):
        self.project_root = project_root
        self.state_root = state_root
        self.network = network

    @property
    def description(self):
        return 'Run terminal commands in per-invocation Linux PID namespaces over an explicitly mounted project.'

    @property
    def default_cwd(self):
        return '/workspace'

    @property
    def skip_container_guards(self):
        return False  # The selected project is a writable host mount; preserve core approvals.

    @property
    def env_description(self):
        return 'a Linux namespace with the configured host project mounted at /workspace'

    def _paths(self, *, create=False):
        if not self.project_root or not self.state_root:
            raise ValueError('configure project_root and state_root in plugins.entries.owned-process.settings')
        project = canonical_directory(self.project_root)
        state = canonical_directory(self.state_root, create=create)
        if project == state or project in state.parents or state in project.parents:
            raise ValueError('project_root and state_root must be disjoint directories')
        protect_runtime(project, state)
        return project, state

    def is_available(self):
        try:
            self._paths()
            return (sys.platform.startswith('linux') and self.network in ('private', 'hostnet')
                    and shutil.which('bwrap') == '/usr/bin/bwrap' and shutil.which('unshare') == '/usr/bin/unshare'
                    and shutil.which('python3', path='/usr/bin:/bin') is not None)
        except (OSError, ValueError, TypeError):
            return False

    def probe(self):
        if self.is_available():
            return ('ready', '')
        return ('needs_setup', 'Configure existing project_root/state_root and Linux namespace requirements.')

    def check_requirements(self, config):
        self._paths(create=True)
        return self.is_available()

    def setup_instructions(self):
        return ['Set plugins.entries.owned-process.settings.project_root and state_root to explicit disjoint absolute directories.',
                'Select terminal.backend: owned_process and plugins.isolation: in_process; Linux user/PID namespaces, bwrap, unshare and system python3 are required.',
                'Network defaults to private; hostnet is an explicit opt-in. Host project approvals remain active.']

    def create_environment(self, *, cwd, timeout, task_id='default', image=None, container_config=None, **kwargs):
        project, state = self._paths(create=True)
        if cwd != '/workspace' and not cwd.startswith('/workspace/'):
            raise ValueError('owned cwd must be within /workspace')
        if not self.is_available():
            raise RuntimeError('owned_process requirements unavailable; no host fallback')
        return OwnedProjectEnvironment(project, state / uuid.uuid4().hex, self.network, cwd, timeout)


def register(ctx):
    from .protected import ProtectedOwnedProvider
    ctx.register_terminal_environment_provider(OwnedProcessProvider(
        project_root=ctx.get_config('project_root'), state_root=ctx.get_config('state_root'),
        network=ctx.get_config('network', 'private')))
    ctx.register_terminal_environment_provider(ProtectedOwnedProvider(ctx.get_config('protected')))
