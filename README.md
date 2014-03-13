Overview
========
infi.pysync is an rsync-like utility that helps you sync your code repo to a remote machine over sftp, but it has some
extra bells and whistles:
  - It can watch a directory tree for changes and then reflect these changes via sftp (just add -w)
  - It can ignore changes in the source tree or changes on the remote machine (see -s and -t)
    - UNIX path patterns are supported (*, ?, [], basically everything fnmatch supports)
    - The '**' recursive wildcard is supported as well. This will match zero or more path components
  - When doing initial sync, it compares local/remote file changes by size (not by timestamp)
  - It can be "Python-aware" (add -p):
    - Will not copy .pyc files
    - Will remove .pyc files if the .py is removed

Usage
-----
After building it (see below how to use infi.projector) just run pysync --help to see how.

Checking out the code
=====================

Run the following:

    easy_install -U infi.projector
    projector devenv build
