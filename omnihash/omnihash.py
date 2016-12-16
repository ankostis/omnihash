#!/usr/bin/env python
# -*- coding: utf-8 -*-

# Standard Imports
from collections import OrderedDict
import hashlib
import io
import json
import os
import sys

import click
import pkg_resources
import validators

import functools as fnt
import itertools as itt


class DigesterFactories(OrderedDict):
    """
    Implements the inclusion/exclusion logic for registering *digester-factories*.

    This dict contains pairs like this::

        {<ALGO-NAME>: <digester-factory>}

    where a ``<digester-factory>`` are functions like this::

        foo(fsize_or_none) -> digester

    A *digester* must support the following methods:

    - ``update(bytes)``
    - ``hexdigest() -> str``

    .. Note::
       The *algo-names* must alway be given in UPPER.

    """
    def __init__(self, includes, excludes):
        super(DigesterFactories, self).__init__()
        self.includes = includes
        self.excludes = excludes

    def register_if_accepted(self, algo, factory):
        assert algo.isupper(), algo
        if self.is_algo_accepted(algo):
            self[algo] = factory

    def is_algo_accepted(self, algo):
        """
        Invoked by :meth:`register_if_accepted()` or by client BEFORE item-assign, not to create needless dig-factory.

        :param algo:
            The UPPER name of the digester to be used as the key in the registry.
        """
        assert algo.isupper(), algo
        includes = self.includes
        excludes = self.excludes
        is_included = not includes or any(f in algo for f in includes)
        is_excluded = excludes and any(f in algo for f in excludes)

        return is_included and not is_excluded


def git_header(otype, fsize):
    return ("%s %i\0" % (otype, fsize)).encode()


class GitSlurpDigester:
    """
    Produce Git-like hashes for bytes without knowing their size a priori.

    Git SHA1-hashes the file-bytes prefixed with the filesize.
    So when reading STDIN, we have to slurp the bytes to derive their length,
    and hash them afterwards.

    But it's not that we slurp multiple files, just the STDIN once.
    """

    fbytes = b''

    def __init__(self, otype):
        # str
        self.otype = otype

    def update(self, fbytes):
        self.fbytes += fbytes

    def hexdigest(self):
        fsize = len(self.fbytes)
        digester = hashlib.sha1(git_header(self.otype, fsize))
        digester.update(self.fbytes)
        return digester.hexdigest()


def append_git_digesters(digfacts):
    """
    Note that contrary to ``git hash-object`` no unix2dos EOL is done!

    :param digfacts:
    :type digfacts: DigesterFactories
    """

    def git_factory(otype, fsize):
        """If `fsize` is known, chunk-hash file, else it slurps it."""
        if fsize is None:
            digester = GitSlurpDigester(otype)
        else:
            digester = hashlib.sha1(git_header(otype, fsize))

        return digester

    algo_pairs = (('GIT-%s' % otype.upper(), otype) for otype in 'blob commit tag'.split())
    digfacts.update(('GIT-%s' % otype.upper(), fnt.partial(git_factory, otype))
                    for algo, otype in algo_pairs
                    if digfacts.is_algo_accepted(algo))


class LenDigester:
    fsize = 0

    def __init__(self, fsize):
        if fsize is not None:
            self.fsize = -fsize

    def update(self, b):
        if self.fsize >= 0:
            self.fsize += len(b)

    def hexdigest(self):
        if self.fsize < 0:
            self.fsize = -self.fsize
        return str(self.fsize)


class FileIter(object):
    """An iterator that chunks in bytes a file-descriptor, auto-closing it when exhausted."""
    def __init__(self, fd):
        self._fd = fd
        self._iter = iter(lambda: fd.read(io.DEFAULT_BUFFER_SIZE), b'')

    def __iter__(self):
        return self._iter

    def next(self):
        try:
            return self._iter.next()
        except StopIteration:
            self._fd.close()
            raise


##
# CLI
##

@click.command()
@click.argument('hashmes', nargs=-1)
@click.option('-s', is_flag=True, default=False, help="Hash input as string, even if there is a file with that name.")
@click.option('-v', is_flag=True, default=False, help="Show version and quit.")
@click.option('-c', is_flag=True, default=False, help="Calculate CRCs as well.")
@click.option('-f', is_flag=False, default=False, multiple=True,
              help=("Select a family of algorithms: "
                    "include only algos having TEXT in their names."
                    "Use it multiple times to select more families."))
@click.option('-x', is_flag=False, default=False, multiple=True,
              help=("Exclude a family of algorithms: "
                    "skip algos having TEXT in their names."
                    "Use it multiple times to exclude more families."))
@click.option('-m', is_flag=False, default=False, help="Match input string.")
@click.option('-j', is_flag=True, default=False, help="Output result in JSON format.")
@click.pass_context
def main(click_context, hashmes, s, v, c, f, x, m, j):
    """
    If there is a file at `hashme`, read and omnihash that.
    Otherwise, assume `hashme` is a string.
    """

    # Print version and quit
    if v:
        version = pkg_resources.require("omnihash")[0].version
        click.echo(version)
        return

    digfacts = collect_digester_factories(f, x, c)

    results = []
    if not hashmes:
        # If no stdin, just help and quit.
        if not sys.stdin.isatty():
            stdin = click.get_binary_stream('stdin')
            bytechunks = iter(lambda: stdin.read(io.DEFAULT_BUFFER_SIZE), b'')
            if not j:
                click.echo("Hashing " + click.style("standard input", bold=True) + "..", err=True)
            results.append([produce_hashes(None, bytechunks, digfacts, match=m, use_json=j)])
        else:
            print(click_context.get_help())
            return
    else:
        hash_many = len(hashmes) > 1
        for hashme in hashmes:
            result = {}
            data = iterate_bytechunks(hashme, s, j, hash_many)
            if data:
                length, bytechunks = data
                result = produce_hashes(length, bytechunks, digfacts, match=m, use_json=j)
            if result:
                result['NAME'] = hashme
                results.append(result)

    if results and j:
        print(json.dumps(results, indent=4, sort_keys=True))


##
# Main Logic
##

def iterate_bytechunks(hashme, is_string, use_json, hash_many):
    """
    Return iterable bytes and content-length if possible.
    """

    # URL
    if not is_string and validators.url(hashme):
        import requests

        if not use_json:
            click.echo("Hashing content of URL " + click.style(hashme, bold=True) + "..", err=not hash_many)
        try:
            response = requests.get(hashme)
        except requests.exceptions.ConnectionError as e:
            raise ValueError("Not a valid URL. :(")
        except Exception as e:
            raise ValueError("Not a valid URL. {}.".format(e))
        if response.status_code != 200:
            click.echo("Response returned %s. :(" % response.status_code, err=True)
        try:
            fsize = int(response.headers.get('Content-Length'))
        except Exception as ex:
            click.echo("[Could not get response-size due to: %s" % ex, err=True)
            fsize = None
        bytechunks = response.iter_content()
    # File
    elif os.path.exists(hashme) and not is_string:
        if os.path.isdir(hashme):
            if not use_json:
                click.echo(click.style("Skipping", fg="yellow") + " directory " + "'" + hashme + "'..", err=True)
            return None

        if not use_json:
            click.echo("Hashing file " + click.style(hashme, bold=True) + "..", err=not hash_many)
        fsize = os.stat(hashme).st_size
        bytechunks = FileIter(open(hashme, mode='rb'))
    # String
    else:
        if not use_json:
            click.echo("Hashing string " + click.style(hashme, bold=True) + "..", err=not hash_many)
        bhashme = hashme.encode('utf-8')
        fsize = len(bhashme)
        bytechunks = (bhashme, )

    return fsize, bytechunks


def append_hashlib_digesters(digfacts):
    """Apend python-default digesters."""
    def digester_fact(algo_name, fsize):
        # A factory that ignores the `fsize` arg.
        return hashlib.new(algo_name)

    algos = sorted(hashlib.algorithms_available)
    digfacts.update((algo.upper(), fnt.partial(digester_fact, algo))
                    for algo in algos
                    if algo not in digfacts and digfacts.is_algo_accepted(algo.upper()))


def append_crc_digesters(digfacts):
    import crcmod.predefined as crcmod

    class MyCrc(crcmod.PredefinedCrc, object):
        # Overridden just to convert hexdigest() into lower.
        def hexdigest(self):
            return super(MyCrc, self).hexdigest().lower()

    def digester_fact(crc_name, fsize):
        # A factory that ignores the `fsize` arg.
        return MyCrc(crc_name)

    algos = sorted(rec[0].upper() for rec in crcmod._crc_definitions_table)
    digfacts.update((algo, fnt.partial(digester_fact, algo))
                    for algo in algos
                    if digfacts.is_algo_accepted(algo))


def collect_digester_factories(includes, excludes, include_CRCs=False):
    """
    Create and return a dictionary of all our active hash algorithms.

    Each digester is a 2-tuple ``( digester.update_func(bytes), digest_func(digester) -> int)``.
    """
    from omnihash import plugin

    digfacts = DigesterFactories([i.upper() for i in includes],
                                 [i.upper() for i in excludes])

    digfacts.register_if_accepted('LENGTH', LenDigester)
    append_hashlib_digesters(digfacts)
    plugin.append_plugin_digesters(digfacts)
    append_git_digesters(digfacts)
    if include_CRCs:
        append_crc_digesters(digfacts)

    assert all(k.isupper() for k in digfacts.keys()), list(digfacts.keys())

    return digfacts


def produce_hashes(fsize, bytechunks, digfacts, match, use_json=False):
    """
    Given our bytes and our algorithms, calculate and print our hashes.
    """

    # Produce hashes
    streams = itt.tee(bytechunks, len(digfacts))
    batch = zip(streams, digfacts.items())
    results = {}

    match_found = False
    for stream, (algo, fact) in batch:
        digester = fact(fsize)
        for b in stream:
            digester.update(b)

        result = digester.hexdigest()
        if match:
            if match in result:
                echo(algo, result, use_json)
                results[algo] = result
                match_found = True
        else:
            results[algo] = result
            echo(algo, result, use_json)

    if match:
        if not match_found:
            if not use_json:
                click.echo(click.style("No matches", fg='red') + " found!", err=True)

    return results


##
# Util
##

def echo(algo, digest, json=False):
    if not json:
        click.echo('  %-*s%s' % (32, click.style(algo, fg='green') + ':', digest))

##
# Entrypoint
##

if __name__ == '__main__':
    try:
        main()
    except ValueError as ex:
        echo(ex, err=True)
