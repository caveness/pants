# coding=utf-8
# Copyright 2014 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import (absolute_import, division, generators, nested_scopes, print_function,
                        unicode_literals, with_statement)

import logging
import sys

import pkg_resources

from pants.base.build_environment import get_scm, pants_version
from pants.base.build_file import BuildFile
from pants.base.cmd_line_spec_parser import CmdLineSpecParser
from pants.base.exceptions import BuildConfigurationError
from pants.base.file_system_project_tree import FileSystemProjectTree
from pants.base.scm_project_tree import ScmProjectTree
from pants.base.workunit import WorkUnit, WorkUnitLabel
from pants.bin.extension_loader import load_plugins_and_backends
from pants.bin.plugin_resolver import PluginResolver
from pants.bin.repro import Reproducer
from pants.build_graph.address_lookup_error import AddressLookupError
from pants.build_graph.build_file_address_mapper import BuildFileAddressMapper
from pants.build_graph.build_file_parser import BuildFileParser
from pants.build_graph.build_graph import BuildGraph
from pants.engine.round_engine import RoundEngine
from pants.goal.context import Context
from pants.goal.goal import Goal
from pants.goal.run_tracker import RunTracker
from pants.help.help_printer import HelpPrinter
from pants.java.nailgun_executor import NailgunProcessGroup
from pants.logging.setup import setup_logging
from pants.option.global_options import GlobalOptionsRegistrar
from pants.option.options_bootstrapper import OptionsBootstrapper
from pants.pantsd.subsystem.pants_daemon_launcher import PantsDaemonLauncher
from pants.reporting.report import Report
from pants.reporting.reporting import Reporting
from pants.source.source_root import SourceRootConfig
from pants.subsystem.subsystem import Subsystem
from pants.task.task import QuietTaskMixin
from pants.util.filtering import create_filters, wrap_filters


logger = logging.getLogger(__name__)


class OptionsInitializer(object):
  """Initializes global options and logging."""

  def __init__(self, options_bootstrapper=None, working_set=None, exiter=sys.exit):
    """
    :param OptionsBootStrapper options_bootstrapper: An options bootstrapper instance (Optional).
    :param pkg_resources.WorkingSet working_set: The working set of the current run as returned by
                                                 PluginResolver.resolve() (Optional).
    :param func exiter: A function that accepts an exit code value and exits (for tests, Optional).
    """
    self._options_bootstrapper = options_bootstrapper or OptionsBootstrapper()
    self._working_set = working_set or PluginResolver(self._options_bootstrapper).resolve()
    self._exiter = exiter

  def _setup_logging(self, global_options):
    """Sets global logging."""
    # N.B. quiet help says 'Squelches all console output apart from errors'.
    level = 'ERROR' if global_options.quiet else global_options.level.upper()
    setup_logging(level, console_stream=sys.stderr, log_dir=global_options.logdir)

  def _register_options(self, subsystems, options):
    """Registers global options."""
    # Standalone global options.
    GlobalOptionsRegistrar.register_options_on_scope(options)

    # Options for subsystems.
    for subsystem in subsystems:
      subsystem.register_options_on_scope(options)

    # TODO(benjy): Should Goals or the entire goal-running mechanism be a Subsystem?
    for goal in Goal.all():
      # Register task options.
      goal.register_options(options)

  def _setup_options(self, options_bootstrapper, working_set):
    bootstrap_options = options_bootstrapper.get_bootstrap_options()
    global_bootstrap_options = bootstrap_options.for_global_scope()

    if global_bootstrap_options.pants_version != pants_version():
      raise BuildConfigurationError(
        'Version mismatch: Requested version was {}, our version is {}.'.format(
          global_bootstrap_options.pants_version, pants_version()
        )
      )

    # Get logging setup prior to loading backends so that they can log as needed.
    self._setup_logging(global_bootstrap_options)

    # Add any extra paths to python path (e.g., for loading extra source backends).
    for path in global_bootstrap_options.pythonpath:
      sys.path.append(path)
      pkg_resources.fixup_namespace_packages(path)

    # Load plugins and backends.
    plugins = global_bootstrap_options.plugins
    backend_packages = global_bootstrap_options.backend_packages
    build_configuration = load_plugins_and_backends(plugins, working_set, backend_packages)

    # Now that plugins and backends are loaded, we can gather the known scopes.
    known_scope_infos = [GlobalOptionsRegistrar.get_scope_info()]

    # Add scopes for all needed subsystems via a union of all known subsystem sets.
    subsystems = Subsystem.closure(
      GoalRunner.subsystems() | Goal.subsystems() | build_configuration.subsystems()
    )
    for subsystem in subsystems:
      known_scope_infos.append(subsystem.get_scope_info())

    # Add scopes for all tasks in all goals.
    for goal in Goal.all():
      known_scope_infos.extend(filter(None, goal.known_scope_infos()))

    # Now that we have the known scopes we can get the full options.
    options = options_bootstrapper.get_full_options(known_scope_infos)
    self._register_options(subsystems, options)

    # Make the options values available to all subsystems.
    Subsystem.set_options(options)

    return options, build_configuration

  def setup(self):
    return self._setup_options(self._options_bootstrapper, self._working_set)


class ReportingInitializer(object):
  """Starts and provides logged info on the RunTracker and Reporting subsystems."""

  def __init__(self, run_tracker=None, reporting=None):
    self._run_tracker = run_tracker or RunTracker.global_instance()
    self._reporting = reporting or Reporting.global_instance()

  def setup(self):
    """Start up the RunTracker and log reporting details."""
    report = self._reporting.initial_reporting(self._run_tracker)
    self._run_tracker.start(report)

    url = self._run_tracker.run_info.get_info('report_url')
    if url:
      self._run_tracker.log(Report.INFO, 'See a report at: {}'.format(url))
    else:
      self._run_tracker.log(Report.INFO, '(To run a reporting server: ./pants server)')

    return self._run_tracker, self._reporting


class GoalRunnerFactory(object):
  def __init__(self, root_dir, options, build_config, run_tracker, reporting, exiter=sys.exit):
    """
    :param str root_dir: The root directory of the pants workspace (aka the "build root").
    :param Options options: The global, pre-initialized Options instance.
    :param BuildConfiguration build_config: A pre-initialized BuildConfiguration instance.
    :param Runtracker run_tracker: The global, pre-initialized/running RunTracker instance.
    :param Reporting reporting: The global, pre-initialized Reporting instance.
    :param func exiter: A function that accepts an exit code value and exits (for tests, Optional).
    """
    self._root_dir = root_dir
    self._options = options
    self._build_config = build_config
    self._run_tracker = run_tracker
    self._reporting = reporting
    self._exiter = exiter

    self._goals = []
    self._targets = []
    self._requested_goals = self._options.goals
    self._target_specs = self._options.target_specs
    self._help_request = self._options.help_request

    self._global_options = options.for_global_scope()
    self._tag = self._global_options.tag
    self._fail_fast = self._global_options.fail_fast
    # Will be provided through context.address_mapper.build_ignore_patterns.
    self._spec_excludes = None
    self._explain = self._global_options.explain
    self._kill_nailguns = self._global_options.kill_nailguns

    self._project_tree = self._get_project_tree(self._global_options.build_file_rev)
    self._build_file_parser = BuildFileParser(self._build_config, self._root_dir)
    build_ignore_patterns = self._global_options.ignore_patterns or []
    build_ignore_patterns.extend(BuildFile._spec_excludes_to_gitignore_syntax(self._root_dir,
                                                                              self._global_options.spec_excludes))
    self._address_mapper = BuildFileAddressMapper(self._build_file_parser, self._project_tree, build_ignore_patterns)
    self._build_graph = BuildGraph(self._address_mapper)
    self._spec_parser = CmdLineSpecParser(
      self._root_dir,
      self._address_mapper,
      spec_excludes=self._spec_excludes,
      exclude_target_regexps=self._global_options.exclude_target_regexp
    )

  def _get_project_tree(self, build_file_rev):
    """Creates the project tree for build files for use in a given pants run."""
    if build_file_rev:
      return ScmProjectTree(self._root_dir, get_scm(), build_file_rev)
    else:
      return FileSystemProjectTree(self._root_dir)

  def _expand_goals(self, goals):
    """Check and populate the requested goals for a given run."""
    for goal in goals:
      try:
        self._address_mapper.resolve_spec(goal)
        logger.warning("Command-line argument '{0}' is ambiguous and was assumed to be "
                       "a goal. If this is incorrect, disambiguate it with ./{0}.".format(goal))
      except AddressLookupError:
        pass

    if self._help_request:
      help_printer = HelpPrinter(self._options)
      result = help_printer.print_help()
      self._exiter(result)

    self._goals.extend([Goal.by_name(goal) for goal in goals])

  def _expand_specs(self, specs, fail_fast):
    """Populate the BuildGraph and target list from a set of input specs."""
    with self._run_tracker.new_workunit(name='parse', labels=[WorkUnitLabel.SETUP]):
      def filter_for_tag(tag):
        return lambda target: tag in map(str, target.tags)

      tag_filter = wrap_filters(create_filters(self._tag, filter_for_tag))

      for spec in specs:
        for address in self._spec_parser.parse_addresses(spec, fail_fast):
          self._build_graph.inject_address_closure(address)
          target = self._build_graph.get_target(address)
          if tag_filter(target):
            self._targets.append(target)

  def _maybe_launch_pantsd(self):
    """Launches pantsd if configured to do so."""
    if self._global_options.enable_pantsd:
      # Avoid runtracker output if pantsd is disabled. Otherwise, show up to inform the user its on.
      with self._run_tracker.new_workunit(name='pantsd', labels=[WorkUnitLabel.SETUP]):
        PantsDaemonLauncher.global_instance().maybe_launch()

  def _is_quiet(self):
    return any(goal.has_task_of_type(QuietTaskMixin) for goal in self._goals) or self._explain

  def _setup_context(self):
    self._maybe_launch_pantsd()

    with self._run_tracker.new_workunit(name='setup', labels=[WorkUnitLabel.SETUP]):
      self._expand_goals(self._requested_goals)
      self._expand_specs(self._target_specs, self._fail_fast)

      # Now that we've parsed the bootstrap BUILD files, and know about the SCM system.
      self._run_tracker.run_info.add_scm_info()

      # Update the Reporting settings now that we have options and goal info.
      invalidation_report = self._reporting.update_reporting(self._global_options,
                                                             self._is_quiet(),
                                                             self._run_tracker)

      context = Context(options=self._options,
                        run_tracker=self._run_tracker,
                        target_roots=self._targets,
                        requested_goals=self._requested_goals,
                        build_graph=self._build_graph,
                        build_file_parser=self._build_file_parser,
                        address_mapper=self._address_mapper,
                        spec_excludes=self._spec_excludes,
                        invalidation_report=invalidation_report)

    return context, invalidation_report

  def setup(self):
    context, invalidation_report = self._setup_context()
    return GoalRunner(context=context,
                      goals=self._goals,
                      kill_nailguns=self._kill_nailguns,
                      run_tracker=self._run_tracker,
                      invalidation_report=invalidation_report,
                      exiter=self._exiter)


class GoalRunner(object):
  """Lists installed goals or else executes a named goal."""

  Factory = GoalRunnerFactory

  def __init__(self, context, goals, run_tracker, invalidation_report, kill_nailguns,
               exiter=sys.exit):
    """
    :param Context context: The global, pre-initialized Context as created by GoalRunnerFactory.
    :param list[Goal] goals: The list of goals to act on.
    :param Runtracker run_tracker: The global, pre-initialized/running RunTracker instance.
    :param InvalidationReport invalidation_report: An InvalidationReport instance (Optional).
    :param bool kill_nailguns: Whether or not to kill nailguns after the run.
    :param func exiter: A function that accepts an exit code value and exits (for tests, Optional).
    """
    self._context = context
    self._goals = goals
    self._run_tracker = run_tracker
    self._invalidation_report = invalidation_report
    self._kill_nailguns = kill_nailguns
    self._exiter = exiter

  @classmethod
  def subsystems(cls):
    # Subsystems used outside of any task.
    return {SourceRootConfig, Reporting, Reproducer, RunTracker, PantsDaemonLauncher}

  def _execute_engine(self):
    workdir = self._context.options.for_global_scope().pants_workdir
    if not workdir.endswith('.pants.d'):
      self._context.log.error('Pants working directory should end with \'.pants.d\', currently it is {}\n'
                              .format(workdir))
      return 1

    unknown_goals = [goal.name for goal in self._goals if not goal.ordered_task_names()]
    if unknown_goals:
      self._context.log.error('Unknown goal(s): {}\n'.format(' '.join(unknown_goals)))
      return 1

    engine = RoundEngine()
    result = engine.execute(self._context, self._goals)

    if self._invalidation_report:
      self._invalidation_report.report()

    return result

  def run(self):
    should_kill_nailguns = self._kill_nailguns

    try:
      result = self._execute_engine()
      if result:
        self._run_tracker.set_root_outcome(WorkUnit.FAILURE)
    except KeyboardInterrupt:
      self._run_tracker.set_root_outcome(WorkUnit.FAILURE)
      # On ctrl-c we always kill nailguns, otherwise they might keep running
      # some heavyweight compilation and gum up the system during a subsequent run.
      should_kill_nailguns = True
      raise
    except Exception:
      self._run_tracker.set_root_outcome(WorkUnit.FAILURE)
      raise
    finally:
      # Must kill nailguns only after run_tracker.end() is called, otherwise there may still
      # be pending background work that needs a nailgun.
      if should_kill_nailguns:
        # TODO: This is JVM-specific and really doesn't belong here.
        # TODO: Make this more selective? Only kill nailguns that affect state?
        # E.g., checkstyle may not need to be killed.
        NailgunProcessGroup().killall()

    return result
