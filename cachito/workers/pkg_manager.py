# SPDX-License-Identifier: GPL-3.0-or-later
import logging
import os
import shutil
import subprocess
import tarfile
import tempfile

import requests

from cachito.errors import CachitoError
from cachito.workers.config import get_worker_config


log = logging.getLogger(__name__)


class GoCacheTemporaryDirectory(tempfile.TemporaryDirectory):
    """
    A wrapper around the TemporaryDirectory context manager to also run `go clean -modcache`.

    The files in the Go cache are read-only by default and cause the default clean up behavior of
    tempfile.TemporaryDirectory to fail with a permission error. A way around this is to run
    `go clean -modcache` before the default clean up behavior is run.
    """
    def __exit__(self, exc, value, tb):
        """
        Clean up temporary directory by first cleaning up the Go cache.
        """
        try:
            env = {'GOPATH': self.name, 'GOCACHE': self.name}
            _run_cmd(('go', 'clean', '-modcache'), {'env': env})
        finally:
            super().__exit__(exc, value, tb)


def resolve_gomod_deps(archive_path, request_id=None):
    """
    Resolve and fetch gomod dependencies for given app source archive.

    :param str archive_path: the full path to the application source code
    :param int request_id: the request ID of the bundle to add the gomod deps to; if not set, this
        step will be skipped
    :return: a list of dictionaries representing the gomod dependencies
    :rtype: list
    :raises CachitoError: if fetching dependencies fails
    """
    worker_config = get_worker_config()
    with GoCacheTemporaryDirectory(prefix='cachito-') as temp_dir:
        source_dir = _extract_app_src(archive_path, temp_dir)

        env = {
            'GOPATH': temp_dir,
            'GO111MODULE': 'on',
            'GOCACHE': temp_dir,
            'GOPROXY': worker_config.cachito_athens_url,
            'PATH': os.environ.get('PATH', ''),
        }

        run_params = {'env': env, 'cwd': source_dir}

        _run_cmd(('go', 'mod', 'download'), run_params)
        go_list_output = _run_cmd(
            ('go', 'list', '-m', '-f', '{{.Path}} {{.Version}}', 'all'), run_params)

        deps = []
        for line in go_list_output.splitlines():
            parts = line.split(' ')
            if len(parts) == 1:
                # This is the application itself, not a dependency
                continue
            if len(parts) > 2:
                log.warning('Unexpected go module output: %s', line)
                continue
            if len(parts) == 2:
                deps.append({'type': 'gomod', 'name': parts[0], 'version': parts[1]})

        # Add the gomod cache to the bundle the user will later download
        if request_id is not None:
            cache_path = os.path.join('pkg', 'mod', 'cache', 'download')
            src_cache_path = os.path.join(temp_dir, cache_path)
            dest_cache_path = os.path.join('gomod', cache_path)
            add_deps_to_bundle_archive(request_id, archive_path, src_cache_path, dest_cache_path)

        return deps


def update_request_with_deps(request_id, deps):
    """
    Update the Cachito request with the resolved dependencies.

    :param int request_id: the ID of the Cachito request
    :param list deps: the list of dependency dictionaries to record
    :raise CachitoError: if the request to the Cachito API fails
    """
    # Import this here to avoid a circular import
    from cachito.workers.requests import requests_auth_session
    config = get_worker_config()
    request_url = f'{config.cachito_api_url.rstrip("/")}/requests/{request_id}'

    log.info('Adding %d dependencies to request %d', len(deps), request_id)
    payload = {'dependencies': deps}
    try:
        rv = requests_auth_session.patch(request_url, json=payload, timeout=30)
    except requests.RequestException:
        msg = f'The connection failed when setting the dependencies on request {request_id}'
        log.exception(msg)
        raise CachitoError(msg)

    if not rv.ok:
        log.error(
            'The worker failed to set the dependencies on request %d. The status was %d. '
            'The text was:\n%s',
            request_id, rv.status_code, rv.text,
        )
        raise CachitoError(f'Setting the dependencies on request {request_id} failed')


def add_deps_to_bundle_archive(request_id, app_archive_path, deps_path, dest_cache_path):
    """
    Add the dependencies to the bundle archive to be downloaded by the user.

    :param int request_id: the request the bundle is for
    :param str app_archive_path: the path to the source archive; this is used as the base of the
        bundle archive if it doesn't exist
    :param str deps_path: the path to the dependencies to add to the bundle archive
    :param str dest_cache_path: the path in the "deps" directory in the bundle to add the content of
        deps_path to
    """
    config = get_worker_config()
    if not os.path.exists(config.cachito_bundles_dir):
        log.debug('Creating %s', config.cachito_bundles_dir)
        os.mkdir(config.cachito_bundles_dir)

    bundle_archive_path = os.path.join(config.cachito_bundles_dir, f'{request_id}.tar.gz')
    if os.path.exists(bundle_archive_path):
        src_archive_path = bundle_archive_path
    else:
        src_archive_path = app_archive_path
    log.debug('Using %s as the source for adding deps to the bundle', src_archive_path)

    # Python can't append to a compressed tar file, so we must create a copy in a temporary
    # directory before bundle_archive_path is created or overwritten
    with tempfile.TemporaryDirectory(prefix='cachito') as temp_dir:
        tmp_bundle_archive_path = os.path.join(temp_dir, 'bundle.tar.gz')
        with tarfile.open(tmp_bundle_archive_path, mode='w:gz') as bundle_archive:
            # Copy over the existing bundle
            with tarfile.open(src_archive_path, mode='r:*') as src_archive:
                for member in src_archive.getmembers():
                    bundle_archive.addfile(member, src_archive.extractfile(member.name))
            # Add the dependencies to the bundle
            bundle_archive.add(deps_path, os.path.join('deps', dest_cache_path))
        # Create or overwrite the bundle tarball at bundle_archive_path
        log.debug('Copying %s to %s', tmp_bundle_archive_path, bundle_archive_path)
        shutil.copyfile(tmp_bundle_archive_path, bundle_archive_path)


def _extract_app_src(archive_path, parent_dir):
    """
    Helper method to extract application source archive to a directory.

    :param str archive_path: the absolute path to the application source code
    :param str parent_dir: the absolute path to the extract target directory
    :returns: the absolute path of the extracted application source code
    :rtype: str
    """
    with tarfile.open(archive_path, 'r:*') as archive:
        archive.extractall(parent_dir)
    return os.path.join(parent_dir, 'app')


def _run_cmd(cmd, params):
    """
    Run the given command with provided parameters.

    :param iter cmd: iterable representing command to be executed
    :param dict params: keyword parameters for command execution
    :returns: the command output
    :rtype: str
    """
    params.setdefault('capture_output', True)
    params.setdefault('universal_newlines', True)
    params.setdefault('encoding', 'utf-8')

    response = subprocess.run(cmd, **params)

    if response.returncode != 0:
        log.error(
            'Processing gomod dependencies with "%s" failed with: %s',
            ' '.join(cmd),
            response.stderr,
        )
        raise CachitoError('Processing gomod dependencies failed')

    return response.stdout
