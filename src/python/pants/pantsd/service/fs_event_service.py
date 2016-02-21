# coding=utf-8
# Copyright 2015 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import (absolute_import, division, generators, nested_scopes, print_function,
                        unicode_literals, with_statement)

import logging
import os
import traceback

from pants.pantsd.service.pants_service import PantsService
from pants.pantsd.subsystem.watchman_launcher import WatchmanLauncher
from pants.pantsd.watchman import Watchman


class FSEventService(PantsService):
  """Filesystem Event Service.

  This is the primary service coupling to watchman and is responsible for subscribing to and
  reading events from watchman's UNIX socket and firing callbacks in pantsd. Callbacks are
  executed in a configurable threadpool but are generally expected to be short-lived.
  """

  ZERO_DEPTH = ['depth', 'eq', 0]

  def __init__(self, build_root, executor, shutdown_executor=True):
    super(FSEventService, self).__init__()
    self._build_root = os.path.realpath(build_root)
    self._executor = executor
    self._shutdown_executor = shutdown_executor
    self._logger = logging.getLogger(__name__)
    self._handlers = {}

  def terminate(self):
    """An extension of PantsService.terminate() that shuts down the executor if so configured."""
    if self._shutdown_executor:
      self._logger.info('shutting down threadpool')
      self._executor.shutdown()
    super(FSEventService, self).terminate()

  def register_all_files_handler(self, callback, name='all_files'):
    """Registers a subscription for all files under a given watch path.

    :param func callback: the callback to execute on each filesystem event
    :param str name:      the subscription name as used by watchman
    """
    self.register_handler(
      name,
      dict(
        fields=['name'],
        expression=[
          'allof',  # All of the below rules must be true to match.
          ['type', 'f'],  # Match only files (not dirs, symlinks, character devices etc).
          ['not', ['dirname', 'dist', self.ZERO_DEPTH]],  # Exclude the ./dist dir.
          # N.B. 'wholename' ensures we match against the absolute ('/x/y/z') vs base path ('z').
          ['not', ['match', '.*', 'wholename']],  # Exclude files in dotpath dirs (.pants.d/* etc).
          ['not', ['match', '*.pyc']]  # Exclude .pyc files.
          # TODO(kwlzn): Make exclusions here optionable.
        ]
      ),
      callback
    )

  def register_simple_handler(self, filename, callback):
    """Registers a simple subscription and handler for files matching a specific name.

    :param str filename: the filename/glob for the simple filename match (e.g. 'BUILD.*').
    :param func callback: the callback to execute on each filesystem event
    """
    self.register_handler(
      filename,
      dict(
        fields=['name'],
        expression=[
          'allof',  # All of the below rules must be true to match.
          ['type', 'f'],  # Match only files (not dirs, symlinks, character devices etc).
          ['match', filename]  # Match only files with a specific name.
        ]
      ),
      callback
    )

  def register_handler(self, name, metadata, callback):
    """Register subscriptions and their event handlers.

    :param str name:      the subscription name as used by watchman
    :param dict metadata: a dictionary of metadata to be serialized and passed to the watchman
                          subscribe command. this should include the match expression as well
                          as any required callback fields.
    :param func callback: the callback to execute on each matching filesystem event
    """
    assert name not in self._handlers, 'duplicate handler name: {}'.format(name)
    assert (
      isinstance(metadata, dict) and 'fields' in metadata and 'expression' in metadata
    ), 'invalid handler metadata!'
    self._handlers[name] = Watchman.EventHandler(name=name, metadata=metadata, callback=callback)

  def fire_callback(self, handler_name, event_data):
    """Fire an event callback for a given handler."""
    return self._handlers[handler_name].callback(event_data)

  def run(self):
    """Main service entrypoint. Called via Thread.start() via PantsDaemon.run()."""
    # Launch Watchman.
    watchman = WatchmanLauncher.global_instance().maybe_launch()

    if not (watchman and watchman.is_alive()):
      raise self.ServiceError('failed to start watchman, bailing!')

    # Enable watchman for the build root.
    watchman.watch_project(self._build_root)

    futures = {}
    id_counter = 0
    subscriptions = self._handlers.values()

    # Setup subscriptions and begin the main event firing loop.
    for handler_name, event_data in watchman.subscribed(self._build_root, subscriptions):
      # On death, break from the loop and contextmgr to terminate callback threads.
      if self.is_killed: break

      if event_data:
        # As we receive events from watchman, submit them asynchronously to the executor.
        future = self._executor.submit(self.fire_callback, handler_name, event_data)
        futures[future] = handler_name

      # Process and log results for completed futures.
      for completed_future in [future for future in futures if future.done()]:
        handler_name = futures.pop(completed_future)
        id_counter += 1

        try:
          result = completed_future.result()
        except Exception:
          result = traceback.format_exc()

        if result:
          # Truthy results or those that raise exceptions are treated as failures.
          self._logger.warning('callback ID {} for {} failed: {}'
                               .format(id_counter, handler_name, result))
        else:
          self._logger.debug('callback ID {} for {} succeeded'.format(id_counter, handler_name))
