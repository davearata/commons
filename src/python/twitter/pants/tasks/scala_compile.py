# ==================================================================================================
# Copyright 2011 Twitter, Inc.
# --------------------------------------------------------------------------------------------------
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this work except in compliance with the License.
# You may obtain a copy of the License in the LICENSE file, or at:
#
#  http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ===================================================================================================

import itertools
import os
import uuid
from twitter.common import contextutil
from twitter.common.dirutil import safe_mkdir, safe_rmtree

from twitter.pants import has_sources, is_scalac_plugin, get_buildroot
from twitter.pants.base import Target
from twitter.pants.base.worker_pool import Work
from twitter.pants.cache import CombinedArtifactCache
from twitter.pants.cache.transforming_artifact_cache import TransformingArtifactCache
from twitter.pants.goal.workunit import WorkUnit
from twitter.pants.targets import resolve_target_sources
from twitter.pants.targets.scala_library import ScalaLibrary
from twitter.pants.targets.scala_tests import ScalaTests
from twitter.pants.tasks import TaskError, Task
from twitter.pants.tasks.jvm_dependency_cache import JvmDependencyCache
from twitter.pants.tasks.nailgun_task import NailgunTask
from twitter.pants.reporting.reporting_utils import items_to_report_element
from twitter.pants.tasks.scala.zinc_analysis_file import ZincAnalysisCollection
from twitter.pants.tasks.scala.zinc_utils import ZincUtils


class ScalaCompile(NailgunTask):
  @classmethod
  def setup_parser(cls, option_group, args, mkflag):
    NailgunTask.setup_parser(option_group, args, mkflag)

    option_group.add_option(mkflag('warnings'), mkflag('warnings', negate=True),
                            dest='scala_compile_warnings', default=True,
                            action='callback', callback=mkflag.set_bool,
                            help='[%default] Compile scala code with all configured warnings '
                                 'enabled.')

    option_group.add_option(mkflag('plugins'), dest='plugins', default=None,
      action='append', help='Use these scalac plugins. Default is set in pants.ini.')

    option_group.add_option(mkflag('partition-size-hint'), dest='scala_compile_partition_size_hint',
      action='store', type='int', default=-1,
      help='Roughly how many source files to attempt to compile together. Set to a large number ' \
           'to compile all sources together. Set this to 0 to compile target-by-target. ' \
           'Default is set in pants.ini.')

    JvmDependencyCache.setup_parser(option_group, args, mkflag)


  def __init__(self, context):
    NailgunTask.__init__(self, context, workdir=context.config.get('scala-compile', 'nailgun_dir'))

    # Set up the zinc utils.
    color = not context.options.no_color
    self._zinc_utils = ZincUtils(context=context, nailgun_task=self, color=color)

    # The rough number of source files to build in each compiler pass.
    self._partition_size_hint = (context.options.scala_compile_partition_size_hint
                                 if context.options.scala_compile_partition_size_hint != -1
                                 else context.config.getint('scala-compile', 'partition_size_hint',
                                                            default=1000))

    # Set up dep checking if needed.
    if context.options.scala_check_missing_deps:
      JvmDependencyCache.init_product_requirements(self)

    self._opts = context.config.getlist('scala-compile', 'args')
    if context.options.scala_compile_warnings:
      self._opts.extend(context.config.getlist('scala-compile', 'warning_args'))
    else:
      self._opts.extend(context.config.getlist('scala-compile', 'no_warning_args'))

    # Various output directories.
    workdir = context.config.get('scala-compile', 'workdir')
    self._classes_dir = os.path.join(workdir, 'classes')
    self._analysis_dir = os.path.join(workdir, 'analysis')

    safe_mkdir(self._classes_dir)
    safe_mkdir(self._analysis_dir)

    self._analysis_file = os.path.join(self._analysis_dir, 'global_analysis.valid')
    self._invalid_analysis_file = os.path.join(self._analysis_dir, 'global_analysis.invalid')
    self._resources_dir = os.path.join(workdir, 'resources')

    # The ivy confs for which we're building.
    self._confs = context.config.getlist('scala-compile', 'confs')

    self.context.products.require_data('exclusives_groups')

    self._local_artifact_cache_spec = \
      context.config.getlist('scala-compile', 'local_artifact_caches2', default=[])
    self._remote_artifact_cache_spec = \
      context.config.getlist('scala-compile', 'remote_artifact_caches2', default=[])

    # A temporary, but well-known, dir to munge analysis files in before caching. It must be
    # well-known so we know where to find the files when we retrieve them from the cache.
    self._analysis_tmpdir = os.path.join(self._analysis_dir, 'artifact_cache_tmpdir')

    # If we are compiling scala libraries with circular deps on java libraries we need to make sure
    # those cycle deps are present.
    self._inject_java_cycles()

    # Sources present in the last analysis that have since been deleted.
    # Generated lazily, so do not access directly. Call self._get_deleted_sources().
    self._deleted_sources = None

  def _inject_java_cycles(self):
    for scala_target in self.context.targets(lambda t: isinstance(t, ScalaLibrary)):
      for java_target in scala_target.java_sources:
        self.context.add_target(java_target)

  def product_type(self):
    return 'classes'

  def can_dry_run(self):
    return True

  def get_artifact_cache(self):
    if self._artifact_cache is None:
      local_cache = self.create_artifact_cache(self._local_artifact_cache_spec)
      remote_cache = self.create_artifact_cache(self._remote_artifact_cache_spec)
      if remote_cache:
        remote_cache = TransformingArtifactCache(remote_cache,
                                                 pre_write_func=self._relativize_artifact,
                                                 post_read_func=self._localize_artifact)
      caches = filter(None, [local_cache, remote_cache])
      self._artifact_cache = CombinedArtifactCache(caches) if caches else None
    return self._artifact_cache

  def _relativize_artifact(self, paths):
    new_paths = []
    for path in paths:
      if path.endswith('.analysis'):
        portable_analysis = path + '.portable'
        if self._zinc_utils.relativize_analysis_file(path, portable_analysis):
          self.context.log.info('Zinc failed to relativize analysis file: %s. '
                                'Will not cache artifact. ' % path)
          return None
        new_paths.append(portable_analysis)
      else:
        new_paths.append(path)
    return new_paths

  def _localize_artifact(self, paths):
    new_paths = []
    for path in paths:
      if path.endswith('.analysis.portable'):
        analysis = path[:-9]
        if self._zinc_utils.localize_analysis_file(path, analysis):
          self.context.log.info('Zinc failed to localize cached analysis file: %s. '
                                'Will not use cached artifact.' % path)
          return None
        os.unlink(path)
        new_paths.append(analysis)
      else:
        new_paths.append(path)
    return new_paths

  def _ensure_analysis_tmpdir(self):
    # Do this lazily, so we don't trigger creation of a worker pool unless we need it.
    if not os.path.exists(self._analysis_tmpdir):
      os.makedirs(self._analysis_tmpdir)
      self.context.background_worker_pool().add_shutdown_hook(lambda: safe_rmtree(self._analysis_tmpdir))

  def _get_deleted_sources(self):
    """Returns the list of sources present in the last analysis that have since been deleted.

    This is a global list. We have no way of associating them to individual targets.
    """
    # We compute the list lazily.
    if self._deleted_sources is None:
      with self.context.new_workunit('find-deleted-sources'):
        analysis = ZincAnalysisCollection(stop_after=ZincAnalysisCollection.PRODUCTS)
        if os.path.exists(self._analysis_file):
          analysis.add_and_parse_file(self._analysis_file, self._classes_dir)
        old_sources = analysis.products.keys()
        self._deleted_sources = filter(lambda x: not os.path.exists(x), old_sources)
    return self._deleted_sources

  def execute(self, targets):
    scala_targets = filter(lambda t: has_sources(t, '.scala'), targets)
    if not scala_targets:
      return

    # Get the exclusives group for the targets to compile.
    # Group guarantees that they'll be a single exclusives key for them.
    egroups = self.context.products.get_data('exclusives_groups')
    group_id = egroups.get_group_key_for_target(scala_targets[0])

    # Add resource dirs to the classpath for us and for downstream tasks.
    for conf in self._confs:
      egroups.update_compatible_classpaths(group_id, [(conf, self._resources_dir)])

    # Get the classpath generated by upstream JVM tasks (including previous calls to execute()).
    cp = egroups.get_classpath_for_group(group_id)

    # Add (only to the local copy) classpath entries necessary for our compiler plugins.
    for conf in self._confs:
      for jar in self._zinc_utils.plugin_jars():
        cp.insert(0, (conf, jar))

    # Invalidation check. Everything inside the with block must succeed for the
    # invalid targets to become valid.
    with self.invalidated(scala_targets, invalidate_dependents=True,
                          partition_size_hint=self._partition_size_hint) as invalidation_check:
      if invalidation_check.invalid_vts and not self.dry_run:
        invalid_targets = [vt.target for vt in invalidation_check.invalid_vts]
        # The analysis for invalid and deleted sources is no longer valid.
        invalid_sources_by_target = self._compute_sources_by_target(invalid_targets)
        invalid_sources = list(itertools.chain.from_iterable(invalid_sources_by_target.values()))
        deleted_sources = self._get_deleted_sources()

        # Work in a tmpdir so we don't stomp the main analysis files on error.
        # The tmpdir is cleaned up in a shutdown hook, because background work
        # may need to access files we create here even after this method returns.
        self._ensure_analysis_tmpdir()
        tmpdir = os.path.join(self._analysis_tmpdir, str(uuid.uuid4()))
        os.mkdir(tmpdir)
        valid_analysis_tmp = os.path.join(tmpdir, 'valid_analysis')
        newly_invalid_analysis_tmp = os.path.join(tmpdir, 'newly_invalid_analysis')
        invalid_analysis_tmp = os.path.join(tmpdir, 'invalid_analysis')
        if ZincUtils.is_nonempty_analysis(self._analysis_file):
          with self.context.new_workunit(name='prepare-analysis'):
            if self._zinc_utils.run_zinc_split(self._analysis_file,
                                               ((invalid_sources + deleted_sources, newly_invalid_analysis_tmp),
                                                ([], valid_analysis_tmp))):
              raise TaskError('Failed to split off invalid analysis.')
            if ZincUtils.is_nonempty_analysis(self._invalid_analysis_file):
              if self._zinc_utils.run_zinc_merge([self._invalid_analysis_file, newly_invalid_analysis_tmp],
                                                 invalid_analysis_tmp):
                raise TaskError('Failed to merge prior and current invalid analysis.')
            else:
              invalid_analysis_tmp = newly_invalid_analysis_tmp

            # Now it's OK to overwrite the main analysis files with the new state.
            ZincUtils._move_analysis(valid_analysis_tmp, self._analysis_file)
            ZincUtils._move_analysis(invalid_analysis_tmp, self._invalid_analysis_file)

        # Figure out the sources and analysis belonging to each partition.
        partitions = []  # Each element is a triple (vts, sources_by_target, analysis).
        for vts in invalidation_check.invalid_vts_partitioned:
          partition_tmpdir = os.path.join(tmpdir, Target.maybe_readable_identify(vts.targets))
          os.mkdir(partition_tmpdir)
          sources = list(itertools.chain.from_iterable(
            [invalid_sources_by_target.get(t, []) for t in vts.targets]))
          analysis_file = os.path.join(partition_tmpdir, 'analysis')
          partitions.append((vts, sources, analysis_file))

        # Split per-partition files out of the global invalid analysis.
        if ZincUtils.is_nonempty_analysis(self._invalid_analysis_file) and partitions:
          with self.context.new_workunit(name='partition-analysis'):
            splits = [(x[1], x[2]) for x in partitions]
            if self._zinc_utils.run_zinc_split(self._invalid_analysis_file, splits):
              raise TaskError('Failed to split invalid analysis into per-partition files.')

        # Now compile partitions one by one.
        for partition in partitions:
          (vts, sources, analysis_file) = partition
          self._process_target_partition(partition, cp)
          # No exception was thrown, therefore the compile succeded and analysis_file is now valid.

          if os.path.exists(analysis_file):  # The compilation created an analysis.
            # Kick off the background artifact cache write.
            if self.get_artifact_cache() and self.context.options.write_to_artifact_cache:
              self._write_to_artifact_cache(analysis_file, vts, invalid_sources_by_target)

            # Merge the newly-valid analysis into our global valid analysis.
            if ZincUtils.is_nonempty_analysis(self._analysis_file):
              with self.context.new_workunit(name='update-upstream-analysis'):
                new_valid_analysis = analysis_file + '.valid.new'
                if self._zinc_utils.run_zinc_merge([self._analysis_file, analysis_file], new_valid_analysis):
                  raise TaskError('Failed to merge new analysis back into valid analysis file.')
              ZincUtils._move_analysis(new_valid_analysis, self._analysis_file)
            else:  # We need to keep analysis_file around. Background tasks may need it.
              ZincUtils._copy_analysis(analysis_file, self._analysis_file)

          if ZincUtils.is_nonempty_analysis(self._invalid_analysis_file):
            with self.context.new_workunit(name='trim-downstream-analysis'):
              # Trim out the newly-valid sources from our global invalid analysis.
              new_invalid_analysis = analysis_file + '.invalid.new'
              discarded_invalid_analysis = analysis_file + '.invalid.discard'
              if self._zinc_utils.run_zinc_split(self._invalid_analysis_file,
                  [(sources, discarded_invalid_analysis), ([], new_invalid_analysis)]):
                raise TaskError('Failed to trim invalid analysis file.')
              ZincUtils._move_analysis(new_invalid_analysis, self._invalid_analysis_file)

          # Now that all the analysis accounting is complete, we can safely mark the
          # targets as valid.
          vts.update()

        # Check for missing dependencies, if needed.
        if invalidation_check.invalid_vts and os.path.exists(self._analysis_file):
          deps_cache = JvmDependencyCache(self.context, scala_targets, self._analysis_file, self._classes_dir)
          deps_cache.check_undeclared_dependencies()

    # Provide the target->class and source->class mappings to downstream tasks if needed.
    if self.context.products.isrequired('classes'):
      sources_by_target = self._compute_sources_by_target(scala_targets)
      classes_by_source = self._compute_classes_by_source()
      self._add_all_products_to_genmap(sources_by_target, classes_by_source)

    # Update the classpath for downstream tasks.
    for conf in self._confs:
      egroups.update_compatible_classpaths(group_id, [(conf, self._classes_dir)])

  @staticmethod
  def _analysis_for_target(analysis_dir, target):
    return os.path.join(analysis_dir, target.id + '.analysis')

  @staticmethod
  def _portable_analysis_for_target(analysis_dir, target):
    return ScalaCompile._analysis_for_target(analysis_dir, target) + '.portable'

  def _write_to_artifact_cache(self, analysis_file, vts, sources_by_target):
    vt_by_target = dict([(vt.target, vt) for vt in vts.versioned_targets])

    # Copy the analysis file, so we can work on it without it changing under us.
    classes_by_source = self._compute_classes_by_source(analysis_file)

    # Set up args for splitting the analysis into per-target files.
    splits = [(sources_by_target.get(t, []), ScalaCompile._analysis_for_target(self._analysis_tmpdir, t))
              for t in vts.targets]
    splits_args_tuples = [(analysis_file, splits)]

    # Set up args for artifact cache updating.
    vts_artifactfiles_pairs = []
    for target, sources in sources_by_target.items():
      artifacts = []
      for source in sources:
        for cls in classes_by_source.get(source, []):
          artifacts.append(os.path.join(self._classes_dir, cls))
      vt = vt_by_target.get(target)
      if vt is not None:
        analysis_file = \
          ScalaCompile._analysis_for_target(self._analysis_tmpdir, target)
        # NOTE: analysis_file doesn't exist yet.
        # We stick the relations file in the artifact as well, for ease of debugging.
        # It's not needed for correctness.
        vts_artifactfiles_pairs.append((vt, artifacts + [analysis_file, analysis_file + '.relations']))

    def split(analysis_file, splits):
      if self._zinc_utils.run_zinc_split(analysis_file, splits):
        raise TaskError('Zinc failed to split analysis file: %s' % analysis_file)

    update_artifact_cache_work = \
      self.get_update_artifact_cache_work(vts_artifactfiles_pairs)
    if update_artifact_cache_work:
      work_chain = [
        Work(split, splits_args_tuples, 'split'),
        update_artifact_cache_work
      ]
      with self.context.new_workunit(name='cache', labels=[WorkUnit.MULTITOOL],
          parent=self.context.run_tracker.get_background_root_workunit()) as parent:
        self.context.submit_background_work_chain(work_chain, workunit_parent=parent)

  def check_artifact_cache(self, vts):
    # Special handling for scala analysis files. Class files are retrieved directly into their
    # final locations in the global classes dir.

    def post_process_cached_vts(cached_vts):
      # Merge the localized analysis with the global one (if any).
      analyses_to_merge = []
      for vt in cached_vts:
        for target in vt.targets:
          analysis_file = ScalaCompile._analysis_for_target(self._analysis_tmpdir, target)
          if os.path.exists(analysis_file):
            analyses_to_merge.append(analysis_file)

      if len(analyses_to_merge) > 0:
        if os.path.exists(self._analysis_file):
          analyses_to_merge.append(self._analysis_file)
        with contextutil.temporary_dir() as tmpdir:
          tmp_analysis = os.path.join(tmpdir, 'analysis')
          if self._zinc_utils.run_zinc_merge(analyses_to_merge, tmp_analysis):
            raise TaskError('Zinc failed to merge cached analysis files.')
          ZincUtils._copy_analysis(tmp_analysis, self._analysis_file)

    self._ensure_analysis_tmpdir()
    return Task.do_check_artifact_cache(self, vts, post_process_cached_vts=post_process_cached_vts)

  def _process_target_partition(self, partition, cp):
    """Needs invoking only on invalid targets.

    partition - a triple (vts, sources_by_target, analysis_file).

    May be invoked concurrently on independent target sets.

    Postcondition: The individual targets in vts are up-to-date, as if each were
                   compiled individually.
    """
    (vts, sources, analysis_file) = partition

    if not sources:
      self.context.log.warn('Skipping scala compile for targets with no sources:\n  %s' % vts.targets)
    else:
      # Do some reporting.
      self.context.log.info(
        'Compiling a partition containing ',
        items_to_report_element(sources, 'source'),
        ' in ',
        items_to_report_element([t.address.reference() for t in vts.targets], 'target'), '.')
      classpath = [entry for conf, entry in cp if conf in self._confs]
      with self.context.new_workunit('compile'):
        # Zinc may delete classfiles, then later exit on a compilation error. Then if the
        # change triggering the error is reverted, we won't rebuild to restore the missing
        # classfiles. So we force-invalidate here, to be on the safe side.
        # TODO: Do we still need this? Zinc has a safe mode now, but it might be very expensive,
        # as it backs up class files.
        vts.force_invalidate()

        # We have to treat our output dir as an upstream element, so zinc can find valid
        # analysis for previous partitions.
        classpath.append(self._classes_dir)
        upstream = { self._classes_dir: self._analysis_file }
        if self._zinc_utils.compile(classpath, sources, self._classes_dir, analysis_file, upstream):
          raise TaskError('Compile failed.')

  def _compute_sources_by_target(self, targets):
    def calculate_sources(target):
      sources = []
      srcs = \
        [os.path.join(target.target_base, src) for src in target.sources if src.endswith('.scala')]
      sources.extend(srcs)
      if (isinstance(target, ScalaLibrary) or isinstance(target, ScalaTests)) and target.java_sources:
        sources.extend(resolve_target_sources(target.java_sources, '.java'))
      return sources
    return dict([(t, calculate_sources(t)) for t in targets])

  def _compute_classes_by_source(self, analysis_file=None):
    """Compute src->classes."""
    if analysis_file is None:
      analysis_file = self._analysis_file

    if not os.path.exists(analysis_file):
      return {}
    len_rel_classes_dir = len(self._classes_dir) - len(get_buildroot())
    analysis = ZincAnalysisCollection(stop_after=ZincAnalysisCollection.PRODUCTS)
    analysis.add_and_parse_file(analysis_file, self._classes_dir)
    classes_by_src = {}
    for src, classes in analysis.products.items():
      classes_by_src[src] = [cls[len_rel_classes_dir:] for cls in classes]
    return classes_by_src

  def _add_all_products_to_genmap(self, sources_by_target, classes_by_source):
    # Map generated classes to the owning targets and sources.
    genmap = self.context.products.get('classes')
    for target, sources in sources_by_target.items():
      for source in sources:
        classes = classes_by_source.get(source, [])
        relsrc = os.path.relpath(source, target.target_base)
        genmap.add(relsrc, self._classes_dir, classes)
        genmap.add(target, self._classes_dir, classes)

      # TODO(John Sirois): Map target.resources in the same way
      # Create and Map scala plugin info files to the owning targets.
      if is_scalac_plugin(target) and target.classname:
        basedir, plugin_info_file = self._zinc_utils.write_plugin_info(self._resources_dir, target)
        genmap.add(target, basedir, [plugin_info_file])
