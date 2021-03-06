# coding=utf-8
# Copyright 2015 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import (absolute_import, division, generators, nested_scopes, print_function,
                        unicode_literals, with_statement)

import itertools
import os
from abc import abstractproperty

from pants.engine.exp.addressable import Exactly, SubclassesOf, addressable, addressable_list
from pants.engine.exp.struct import Struct, StructWithDeps
from pants.source.wrapped_globs import Globs, RGlobs, ZGlobs


class Variants(Struct):
  """A struct that holds default variant values.

  Variants are key-value pairs representing uniquely identifying parameters for a Node.

  Default variants are usually configured on a Target to be used whenever they are
  not specified by a caller.

  They can be imagined as a dict in terms of dupe handling, but for easier hashability they are
  stored internally as sorted nested tuples of key-value strings.
  """

  @staticmethod
  def merge(left, right):
    """Merges right over left, ensuring that the return value is a tuple of tuples, or None."""
    if not left:
      if right:
        return tuple(right)
      else:
        return None
    if not right:
      return tuple(left)
    # Merge by key, and then return sorted by key.
    merged = dict(left)
    for key, value in right:
      merged[key] = value
    return tuple(sorted(merged.items(), key=lambda x: x[0]))

  def __init__(self, default=None, **kwargs):
    """
    :param dict default: A dict of default variant values.
    """
    # TODO: enforce the type of variants using the Addressable framework.
    super(Variants, self).__init__(default=default, **kwargs)


class Sources(Struct):
  """Represents a collection of source files.

  Note that because this does not extend `StructWithDeps`, subclasses that would like to have
  dependencies should mix-in StructWithDeps.
  """

  def __init__(self,
               name=None,
               files=None,
               globs=None,
               rglobs=None,
               zglobs=None,
               excludes=None,
               **kwargs):
    """
    :param string name: An optional name of this set of sources if the set is top-level for sharing.
    :param files: A list of relative file paths to include.
    :type files: list of string.
    :param string globs: A relative glob pattern of files to include.
    :param string rglobs: A relative recursive glob pattern of files to include.
    :param string zglobs: A relative zsh-style glob pattern of files to include.
    :param zglobs: A relative zsh-style glob pattern of files to include.
    :param excludes: A set of sources to exclude from the sources gathered via files, globs, rglobs
                     and zglobs.
    :type excludes: :class:`Sources`
    """
    super(Sources, self).__init__(name=name, files=files, globs=globs, rglobs=rglobs, zglobs=zglobs,
                                  **kwargs)
    if files and self.extensions:
      for f in files:
        if not self._accept_file(f):
          # TODO: TargetDefinitionError or similar
          raise ValueError('Path `{}` selected by {} is not a {} file.'.format(
            f, self, self.extensions))
    self.excludes = excludes

  def _accept_file(self, f):
    """Returns true if the given file's extension matches this Sources type."""
    _, ext = os.path.splitext(f)
    return ext in self.extensions

  @abstractproperty
  def extensions(self):
    """A collection of file extensions collected by this Sources instance.

    An empty collection indicates that any extension will be accepted.
    """

  @property
  def excludes(self):
    """The sources to exclude.

    :rtype: :class:`Sources`
    """

  def iter_paths(self, base_path=None):
    """Return an iterator over this collection of sources file paths.

    If these sources are addressable, the paths returned will have a base path of the address
    `spec_path`; otherwise a `base_path` must be explicitly supplied.

    :param string base_path: If this collection of sources is not addressed, the base path in the
                             repo the sources are relative to.
    :returns: An iterator over the source paths that match the file extension and are
              not excluded by `excludes`.  Paths are of the form
              `os.path.join(base_path, rel_path)`.
    :rtype: :class:`collections.Iterator` of string
    """
    base_path = self.address.spec_path if self.address else base_path
    if not base_path:
      raise ValueError('A `base_path` must be supplied to iterate paths for {!r}'.format(self))

    excluded_files = frozenset(self.excludes.iter_paths(base_path)) if self.excludes else ()

    def file_sources():
      if self.files:
        yield self.files
      for spec, fileset_wrapper_type in ((self.globs, Globs),
                                         (self.rglobs, RGlobs),
                                         (self.zglobs, ZGlobs)):
        if spec:
          fileset = fileset_wrapper_type(base_path)(spec)
          yield fileset

    for rel_path in itertools.chain.from_iterable(file_sources()):
      if self._accept_file(rel_path):
        file_path = os.path.join(base_path, rel_path)
        if file_path not in excluded_files:
          yield file_path

# Since Sources.excludes is recursive on the Sources type, we need to post-class-definition
# re-define excludes in this way.
Sources.excludes = addressable(Exactly(Sources))(Sources.excludes)


class Target(Struct):
  """TODO(John Sirois): XXX DOCME"""

  class ConfigurationNotFound(Exception):
    """Indicates a requested configuration of a target could not be found."""

  def __init__(self, name=None, configurations=None, **kwargs):
    """
    :param string name: The name of this target which forms its address in its namespace.
    :param list configurations: The configurations that apply to this target in various contexts.
    """
    super(Target, self).__init__(name=name, **kwargs)

    self.configurations = configurations

  @addressable_list(SubclassesOf(Struct))
  def configurations(self):
    """The configurations that apply to this target in various contexts.

    :rtype list of :class:`pants.engine.exp.configuration.Struct`
    """

  def select_configuration(self, name):
    """Selects a named configuration of this target.

    :param string name: The name of the configuration to select.
    :returns: The configuration with the given name.
    :rtype: :class:`pants.engine.exp.configuration.Struct`
    :raises: :class:`Target.ConfigurationNotFound` if the configuration was not found.
    """
    configs = tuple(config for config in self.configurations if config.name == name)
    if len(configs) != 1:
      configurations = ('{} -> {!r}'.format(repr(c.name) if c.name else '<anonymous>', c)
                        for c in configs)
      raise self.ConfigurationNotFound('Failed to find a single configuration named {!r} for these '
                                       'configurations in {!r}:\n\t{}'
                                       .format(name, self, '\n\t'.join(configurations)))
    return configs[0]

  def select_configuration_type(self, tpe):
    """Selects configurations of the given type on this target.

    :param type tpe: The exact type of the configuration to select: subclasses will not match.
    :returns: The configurations with the given type.
    :rtype: :class:`pants.engine.exp.configuration.Configuration`
    """
    return tuple(config for config in self.configurations if type(config) == tpe)

  def walk_targets(self, postorder=True):
    """Performs a depth first walk of this target, visiting all reachable targets exactly once.

    TODO: Walking a Target graph probably doesn't make sense; but walking an ExecutionGraph does.

    :param bool postorder: When ``True``, the traversal order is postorder (children before
                           parents), else it is preorder (parents before children).
    """
    visited = set()

    def walk(target):
      if target not in visited:
        visited.add(target)
        if not postorder:
          yield target
        for configuration in self.configurations:
          for dep in configuration.dependencies:
            if isinstance(dep, Target):
              for t in walk(dep):
                yield t
        if postorder:
          yield target

    for target in walk(self):
      yield target
