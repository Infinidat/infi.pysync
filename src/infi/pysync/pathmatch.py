import os.path
from fnmatch import fnmatch


def _recursive_path_match(path_list, pattern_list, match_subpath):
    while pattern_list and pattern_list[0] == "**":
        pattern_list.pop(0)

    if not pattern_list:
        return True

    if not path_list:
        return False

    for i in range(len(path_list)):
        if _path_match(path_list[i:], pattern_list, match_subpath):
            return True
    return False


def _path_match(path_list, pattern_list, match_subpath):
    if not path_list or not pattern_list:
        return False

    for i, pattern in enumerate(pattern_list):
        if pattern == "**":
            # recursive matcher.
            return _recursive_path_match(path_list[i:], pattern_list[i + 1:], match_subpath)
        elif len(path_list) <= i:
            return False
        elif not fnmatch(path_list[i], pattern):
                return False
    return True if match_subpath else len(path_list) == i + 1


def path_match(path, pattern, match_subpath=False):
    path = os.path.normpath(path)
    pattern = os.path.normpath(pattern)
    path_list = path.split(os.path.sep)
    pattern_list = pattern.split(os.path.sep)
    return _path_match(path_list, pattern_list, match_subpath)


def find_matching_pattern(path, patterns, match_subpath=False):
    for pattern in patterns:
        if path_match(path, pattern, match_subpath):
            return pattern
    return None


if __name__ == '__main__':
    assert path_match('a/b/c/d', 'a/b/c/d')
    assert not path_match('a/b/c/d', 'a/b/c')
    assert path_match('a/b/c/d', 'a/*/c/d')
    assert not path_match('a/b/c/d', 'a/*/d/d')
    assert path_match('a/b/c/d', 'a/**/d')
    assert path_match('a/b/c/d', '**/d')
    assert path_match('a/b/c/d', '**')
    assert not path_match('a/b/c/d', '**/e')
    assert path_match('a/b/c/d', 'a/**/b/c/d')
    assert path_match('a/b/e.y', '**/*.y')
    assert path_match("a/b/c", "a", match_subpath=True)
