from textwrap import dedent

from cachito.workers.pkg_managers import yarn


def test_get_packages_from_lockfile(tmp_path):
    lockfile_content = dedent(
        """
        # THIS IS AN AUTOGENERATED FILE. DO NOT EDIT THIS FILE DIRECTLY (oops).
        # yarn lockfile v1

        foo:
          version "1.0.0"
        """
    )
    lockfile_path = tmp_path / "yarn.lock"
    lockfile_path.write_text(lockfile_content)

    packages = yarn.get_packages_from_lockfile(lockfile_path)
    assert packages[0].name == "foo"
    assert packages[0].version == "1.0.0"


def test_get_npm_proxy_repo_name():
    assert yarn.get_yarn_proxy_repo_name(3) == "cachito-yarn-3"


def test_get_npm_proxy_repo_url():
    assert yarn.get_yarn_proxy_repo_url(3).endswith("/repository/cachito-yarn-3/")


def test_get_npm_proxy_username():
    assert yarn.get_yarn_proxy_repo_username(3) == "cachito-yarn-3"