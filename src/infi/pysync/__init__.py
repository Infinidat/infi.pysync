#!/usr/bin/env python
"""
Sync Python source files, removes orphan .pyc files and ignores .git directory (or other patterns)

Usage:
  pysync [options] [-s PATTERN...] [-t PATTERN...] TARGET
  pysync [options] [-s PATTERN...] [-t PATTERN...] SOURCE TARGET

Options:
  -h, --help         Show this help
  -v, --verbose      Be verbose
  -n, --dry-run      Don't do anything, report it instead
  -w, --watch        Keep watching for changed files/directories in SOURCE and sync them to TARGET
  -p, --python       Python-aware synchronization:
                       - Don't copy .pyc from SOURCE
                       - Remove .pyc files in TARGET when syncing .py files
                       - Remove .pyc files in TARGET when there's no .py file
  -s PATTERN --skip-source=PATTERN   Don't sync PATTERN from source.
                                     Accepts path patterns (e.g. *, ?, []) and ** as a recursive match-all.
                                     Works well with path hierarchy (e.g. a/b/c.ext), comparison starts from SOURCE.
  -t PATTERN --skip-target=PATTERN   Don't remove objects matching PATTERN from TARGET when they don't exist in SOURCE.
                                     Same rules as -s.
  -b INTERVAL --batch-interval=INTERVAL  Interval in seconds to sync [default: 1]
  -i IDENTITY --identity=IDENTITY    SSH identity file to use when authenticating remote host

Options you probably don't want to use:
  --no-preserve-time                 Don't sync mtime/atime (default: False)
  --no-preserve-permissions          Don't sync st_mode (default: False)
  --size-only                        Sync by size only (default: False)
  --mtime-only                       Sync by modification time only (default: False)
  --perm-only                        Sync by permissions only (default: False)

  TARGET             A scp-like path: [user@]host[:port][:path]
"""
import os
import sys
import errno
import time
import stat
from fnmatch import fnmatch
from docopt import docopt
from watchdog.observers import Observer
from watchdog.events import (FileSystemEventHandler,
                             EVENT_TYPE_MODIFIED, EVENT_TYPE_MOVED, EVENT_TYPE_CREATED, EVENT_TYPE_DELETED)
from watchdog.utils import has_attribute
from Queue import Queue, Empty
from threading import Thread

from .pathmatch import find_matching_pattern
from .sftp import SyncSFTPClient

verbose = None
dry_run = False
source_ignore_patterns = [".git", "conf"]
target_ignore_patterns = ["bin", "eggs", "develop-eggs", "parts", "**/__version__.py*", ".cache"]
python_mode = False
compare_by_size = True
compare_by_mtime = True
compare_by_perm = True


def vprint(msg, *args, **kwargs):
    global verbose
    if verbose:
        print(msg.format(*args, **kwargs))


class SyncWorker(Thread):
    def __init__(self, sftp, queue, source_base_path, target_base_path, batch_interval=1.0):
        self.sftp = sftp
        self.queue = queue
        self.source_base_path = source_base_path
        self.target_base_path = target_base_path
        self.batch_interval = batch_interval
        super(SyncWorker, self).__init__()

    def stop(self):
        self.queue.put(None)

    def run(self):
        try:
            batch_start_time = time.time()
            batch = []
            shutdown = False
            while not shutdown:
                event = None
                try:
                    wait_time = max(0, batch_start_time + self.batch_interval - time.time())
                    event = self.queue.get(timeout=wait_time)
                    self.queue.task_done()
                    if not event:
                        shutdown = True
                except Empty:
                    pass

                now = time.time()
                if event:
                    batch.append(event)
                if (now - batch_start_time) >= self.batch_interval or shutdown:
                    if batch:
                        self._handle_batch(batch)
                    batch_start_time = now
                    batch = []
        except:
            import traceback
            traceback.print_exc()

    def _handle_batch(self, batch):
        paths_synced = set()
        for event in batch:
            if event.event_type == EVENT_TYPE_DELETED:
                self.sftp.rm_rf(self._to_target_path(event.src_path))
                if event.src_path in paths_synced:
                    paths_synced.remove(event.src_path)
                self._remove_pyc_file_if_needed(event.src_path)
            elif event.event_type in (EVENT_TYPE_MODIFIED, EVENT_TYPE_CREATED) and not event.src_path in paths_synced:
                if os.path.exists(event.src_path):
                    self.sftp.put(event.src_path, self._to_target_path(event.src_path))
                    paths_synced.add(event.src_path)
                    self._remove_pyc_file_if_needed(event.src_path)
                else:
                    vprint("not syncing {}, already deleted in source.", event.src_path)
            elif event.event_type == EVENT_TYPE_MOVED:
                if event.src_path in paths_synced:
                    paths_synced.remove(event.src_path)
                if event.dest_path in paths_synced:
                    paths_synced.remove(event.dest_path)
                self.sftp.rename(self._to_target_path(event.src_path), self._to_target_path(event.dest_path))
                self._remove_pyc_file_if_needed(event.src_path)
                self._remove_pyc_file_if_needed(event.dest_path)

    def _remove_pyc_file_if_needed(self, path):
        fname = os.path.basename(path)
        if python_mode and fnmatch(fname, "*.py"):
            vprint("removing .pyc since {} was deleted", path)
            self.sftp.rm_rf(self._to_target_path(path + "c"))

    def _to_target_path(self, path):
        return _convert_path(self.source_base_path, self.target_base_path, path)


class SyncHandler(FileSystemEventHandler):
    def __init__(self, queue, source_base_path, source_ignore_patterns, *args, **kwargs):
        self.queue = queue
        self.source_base_path = os.path.normpath(os.path.abspath(source_base_path))
        self.source_ignore_patterns = source_ignore_patterns
        super(SyncHandler, self).__init__(*args, **kwargs)

    def dispatch(self, event):
        try:
            paths = [event.src_path]
            if has_attribute(event, 'dest_path'):
                paths.append(event.dest_path)

            paths = [os.path.relpath(os.path.normpath(os.path.abspath(path)), start=self.source_base_path) for path in paths]
            event_type_to_name = {EVENT_TYPE_MOVED: "move", EVENT_TYPE_CREATED: "create",
                                  EVENT_TYPE_MODIFIED: "modify", EVENT_TYPE_DELETED: "delete"}
            vprint("local change type: {} paths: {}", event_type_to_name[event.event_type], paths)
            for path in paths:
                pattern = find_matching_pattern(path, self.source_ignore_patterns, match_subpath=True)
                if pattern:
                    vprint("ignoring change for path {}, pattern: {}", path, pattern)
                    return

            self.queue.put(event)
        except:
            import traceback
            traceback.print_exc()


def _sync_file(sftp, source_path, source_stat, remote_path):
    local_size = source_stat.st_size
    local_time = int(source_stat.st_mtime)
    local_perm = source_stat[stat.ST_MODE]
    remote_stat = sftp.stat_or_none(remote_path)
    if remote_stat and not stat.S_ISREG(remote_stat.st_mode):
        raise Exception("remote path {} should be a regular file, but it's not.".format(remote_path))
    remote_size = remote_stat.st_size if remote_stat else None
    remote_time = int(remote_stat.st_mtime) if remote_stat else None
    remote_perm = remote_stat.st_mode if remote_stat else None

    size_diff = local_size != remote_size if compare_by_size else False
    time_diff = local_time != remote_time if compare_by_mtime else False
    perm_diff = local_perm != remote_perm if compare_by_perm else False

    vprint("sync_file comparison for {} -> {}: size={} ({} <=> {}) time={} ({} <=> {})".format(source_path, remote_path,
           size_diff if compare_by_size else "<unused>", local_size, remote_size,
           time_diff if compare_by_mtime else "<unused>", local_time, remote_time,
           perm_diff if compare_by_perm else "<unused>", local_perm, remote_perm))
    if size_diff or time_diff or perm_diff:
        sftp.put(source_path, remote_path)

        if python_mode and source_path.endswith(".py"):
            remote_pyc = remote_path + "c"
            if sftp.stat_or_none(remote_pyc):
                vprint("removing remote .pyc file {}", remote_pyc)
                sftp.rm_rf(remote_pyc)


def filtered_walk(path, ignore_patterns=[], listdir_func=os.listdir, stat_func=os.lstat,
                  listdir_attr_func=None, compare_path=""):
    path = os.path.normpath(path)
    base_path = os.path.relpath(path, compare_path) if compare_path else path

    entries = []

    def _filter_entries(all_entries):
        for entry in all_entries:
            p = os.path.join(base_path, entry)
            pattern = find_matching_pattern(p, ignore_patterns, match_subpath=True)
            if not pattern:
                entries.append(entry)
            else:
                vprint("ignoring {}, ignore pattern: {}", p, pattern)

    if listdir_attr_func:
        stats = listdir_attr_func(path)
        _filter_entries(s.filename for s in stats)
        entry_set = set(entries)
        stats = [s for s in stats if s.filename in entry_set]
    else:
        _filter_entries(listdir_func(path))
        stats = [stat_func(os.path.join(path, e)) for e in entries]

    entry_to_stat = dict(zip(entries, stats))

    yield (path, entry_to_stat)

    for d in [e for e, s in entry_to_stat.items() if stat.S_ISDIR(s.st_mode)]:
        try:
            stat_func(os.path.join(path, d))  # just to make sure it still exists
            for result in filtered_walk(os.path.join(path, d), ignore_patterns, listdir_func, stat_func,
                                        listdir_attr_func, compare_path):
                yield result
        except (OSError, IOError), e:
            if e.errno != errno.ENOENT:
                raise


def _convert_path(from_base_path, to_base_path, path):
    relpath = os.path.relpath(path, start=from_base_path)
    return os.path.normpath(os.path.join(to_base_path, relpath))


def _add_source_to_target(sftp, source_base_path, remote_base_path):
    # Try to see which files/directories are missing remotely or have different size and create/copy.
    for (dirpath, entry_to_stat) in filtered_walk(source_base_path, ignore_patterns=source_ignore_patterns,
                                                  compare_path=source_base_path):
        remote_dirpath = _convert_path(source_base_path, remote_base_path, dirpath)
        sftp.mkdir_if_not_exist(remote_dirpath)

        for dirname in [k for k, s in entry_to_stat.items() if stat.S_ISDIR(s.st_mode)]:
            sftp.mkdir_if_not_exist(os.path.join(remote_dirpath, dirname))

        for fname, f_stat in [(f, s) for f, s in entry_to_stat.items() if stat.S_ISREG(s.st_mode)]:
            if not python_mode or not fnmatch(fname, '*.pyc'):
                _sync_file(sftp, os.path.join(dirpath, fname), f_stat, os.path.join(remote_dirpath, fname))


def _remove_target_when_missing_source(sftp, source_base_path, remote_base_path):
    for (dirpath, entry_to_stat) in filtered_walk(remote_base_path, ignore_patterns=target_ignore_patterns,
                                                  listdir_attr_func=sftp.listdir_attr, stat_func=sftp.stat,
                                                  compare_path=remote_base_path):
        src_dirpath = _convert_path(remote_base_path, source_base_path, dirpath)
        if not os.path.exists(src_dirpath):
            sftp.rm_rf(dirpath)
        else:
            for dirname in [k for k, s in entry_to_stat.items() if stat.S_ISDIR(s.st_mode)]:
                if not os.path.exists(os.path.join(src_dirpath, dirname)):
                    sftp.rm_rf(os.path.join(dirpath, dirname))

            for fname, f_stat in [(f, s) for f, s in entry_to_stat.items() if stat.S_ISREG(s.st_mode)]:
                if python_mode and fnmatch(fname, '*.pyc'):
                    # Make sure there's a .py file in the source.
                    if not os.path.exists(os.path.join(src_dirpath, fname[:-1])):
                        vprint("removing {} file due to non-existent {} file", fname, fname[:-1])
                        sftp.remove(os.path.join(dirpath, fname))
                elif not os.path.exists(os.path.join(src_dirpath, fname)):
                    sftp.remove(os.path.join(dirpath, fname))


def initial_sync(sftp, source_base_path, remote_base_path):
    vprint("syncing source to target")
    _add_source_to_target(sftp, source_base_path, remote_base_path)
    vprint("removing files/directories from target that don't exist in source")
    _remove_target_when_missing_source(sftp, source_base_path, remote_base_path)


def watch_for_changes(sftp, source_base_path, remote_base_path, batch_interval):
    vprint("watching for changes")
    queue = Queue()
    worker = SyncWorker(sftp, queue, source_base_path, remote_base_path, batch_interval)
    worker.start()

    observer = Observer()
    observer.schedule(SyncHandler(queue, source_base_path, source_ignore_patterns), source_base_path, recursive=True)
    observer.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("caught keyboard interrupt, stopping.")
        worker.stop()
        observer.stop()
    observer.join()
    worker.join()


def main(argv=sys.argv[1:]):
    global verbose, python_mode, dry_run, source_ignore_patterns, target_ignore_patterns, compare_by_mtime, compare_by_size
    args = docopt(__doc__, argv=argv)
    source_path = args["SOURCE"] if args["SOURCE"] else "."
    target_path = args["TARGET"]
    verbose = args["--verbose"]
    python_mode = args["--python"]
    dry_run = args["--dry-run"]
    source_ignore_patterns = args["--skip-source"]
    if not source_ignore_patterns:
        source_ignore_patterns = []
    elif not isinstance(source_ignore_patterns, (list, tuple)):
        source_ignore_patterns = [source_ignore_patterns]
    target_ignore_patterns = args["--skip-target"]
    if not target_ignore_patterns:
        target_ignore_patterns = []
    elif not isinstance(target_ignore_patterns, (list, tuple)):
        target_ignore_patterns = [target_ignore_patterns]
    sync_interval = float(args['--batch-interval'])
    compare_by_mtime = not args['--size-only']
    compare_by_size = not args['--mtime-only']
    compare_by_perm = not args['--perm-only']

    if not os.path.exists(source_path):
        sys.stderr.write("error: source path {} does not exist, aborting.\n".format(source_path))
        sys.exit(2)

    if not os.path.isdir(source_path):
        sys.stderr.write("error: source path {} is not a directory, aborting.\n".format(source_path))
        sys.exit(2)

    sftp = SyncSFTPClient.create_sftp_connection(target_path, dry_run, not args['--no-preserve-time'],
                                                 identity_file=args['--identity'], logger_func=vprint,
                                                 preserve_permissions=not args['--no-preserve-permissions'])
    try:
        remote_path = sftp.remote_path()
    except (OSError, IOError), e:
        if e.errno == errno.ENOENT:
            sys.stderr.write("error: target path {} does not exist, aborting.\n".format(sftp.path))
            sys.exit(2)
        else:
            raise

    vprint("source path: {}", source_path)
    vprint("remote path: {}", remote_path)
    vprint("source ignore patterns: {}", source_ignore_patterns)
    vprint("target ignore patterns: {}", target_ignore_patterns)
    vprint("sync interval: {}", sync_interval)

    initial_sync(sftp, source_path, remote_path)

    if args["--watch"]:
        watch_for_changes(sftp, source_path, remote_path, sync_interval)
